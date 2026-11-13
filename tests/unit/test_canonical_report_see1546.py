"""SEE-1546 WP3 PR-C — gqt canonical 双源批式编排单测（§SPEC-037）。

判据先行（TDD RED）：批式臂（gut_full 一次 + acceptance 私有目录一次）、
逐文件 sink `qa-coverage-hits-{uuid}-{file_key8}`、在树插桩备份/恢复零残留、
coverage-union/1 逐文件 union、canonical-report/1 形状（arms=2、run_uuid
不同、union.valid、not_certified、exploratory、performance.acceptance_probe）、
evidence recipe.arm 显式注入（缺省推断保留向后兼容）、失败臂 → union invalid。
"""

from __future__ import annotations

import json
import os
import re
import subprocess as sp
import sys
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
        self.real_run = sp.run  # 构造时的真实 subprocess.run（shadow cp 等需真执行）
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
        # 非测试执行类子进程（compile 探针/cp 建 shadow 等）真实执行
        if "gut_cmdln.gd" not in cmd:
            return self.real_run(*a, **k)
        on_gdir = ".gqt-run-" in cmd  # acceptance 私有套件臂
        # §SPEC-063b：合成 GUT 运行流输出（脚本头 + `* test_name` 行），
        # 使 acceptance 对账五项可按 manifest 判定（R2/R3 流程对账非空转）
        flow = ""
        if on_gdir:
            m = re.search(r"-gdir=res://(\S+)/generated", cmd)
            if m:
                gen = self.root / m.group(1) / "generated"
                parts = []
                for man_path in sorted(gen.glob("*.manifest.json")):
                    man = json.loads(man_path.read_text(encoding="utf-8"))
                    parts.append("res://" + m.group(1) + "/generated/test_"
                                 + man_path.name.replace(
                                     ".manifest.json", ".gd"))
                    for att in man.get("attribution", []):
                        parts.append("* " + att["test_name"])
                flow = "\n".join(parts)
        pg = self.root / "project.godot"
        self.pg_at_gut.append(pg.read_text(encoding="utf-8")
                              if pg.is_file() else "")
        self.flusher_at_gut.append(
            (self.root / "addons" / "gqt_cov_flusher.gd").is_file())
        env = k.get("env") or {}
        xdg = env.get("XDG_DATA_HOME")
        if xdg:
            proj = re.search(r'config/name\s*=\s*"([^"]+)"',
                             pg.read_text(encoding="utf-8")).group(1)
            dest = Path(xdg) / "godot" / "app_userdata" / proj
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest = self.root
        for gd in self.root.glob("src_game/*.gd"):
            src = gd.read_text(encoding="utf-8")
            # runtime-pid sink 形态：`qa-coverage-hits-<uuid>-` +
            # str(OS.get_process_id()) + `-<key8>.txt`
            pid_pairs = re.findall(
                r'qa-coverage-hits-([0-9a-f-]{8,36})-" \+ '
                r'str\(OS\.get_process_id\(\)\) \+ "-([0-9a-f]{8})\.txt',
                src)
            pairs = [(u, k, True) for u, k in pid_pairs]
            for m2 in re.finditer(
                    r'qa-coverage-hits-([0-9a-f-]{8,36})-([0-9a-f]{8})\.txt',
                    src):
                if (m2.group(1), m2.group(2)) not in pid_pairs:
                    pairs.append((m2.group(1), m2.group(2), False))
            for uuid, key8, _pid in pairs:
                sink = (f"qa-coverage-hits-{uuid}-99999-{key8}.txt"
                        if _pid else f"qa-coverage-hits-{uuid}-{key8}.txt")
                # 三种传输形态的探针点数：direct 逐 id 函数 / R3 store_string /
                # R4 内存 flag 标记
                probe_ids = set(re.findall(r"__qa_cov_probe_(\d+)\(\)", src)) | set(
                    re.findall(r"__qa_cov_probe_m_[0-9a-f]{8}\[(\d+)\] = 1", src))
                count = len(probe_ids)
                ids = list(range(count))
                if on_gdir and self.mode == "half_on_gdir":
                    ids = ids[: max(1, count // 2)]
                (dest / sink).write_text(
                    "".join(f"{i}\n" for i in ids), encoding="utf-8")
        class R:
            def __init__(self, rc, err, out=""):
                self.returncode = rc
                self.stdout = out
                self.stderr = err
        return R(self.returncode, self.stderr_text, flow)


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
    from godot_qa_toolkit.canonical.orchestrator import _backup_base
    assert g.glob(str(_backup_base() / "*")) == []  # 备份目录已清


def test_gut_arm_uses_config_face_acceptance_uses_private_dir(
        project: Path, tmp_path: Path):
    recipe, _ = _recipe(project, tmp_path)
    fake = FakeGodot(project)
    sp.run = fake
    run_canonical(project, _targets(project), recipe)
    gut_calls = [c for c in fake.calls if any(
        x == "-s" for x in c) and "gut_cmdln.gd" in " ".join(c)]
    assert len(gut_calls) == 4  # face 1 轮 + acceptance 3 重复（§SPEC-062C）
    gdirs = [" ".join(x for x in c if str(x).startswith("-gdir="))
             for c in gut_calls]
    # 两臂均按分片扇出（-gdir 均在）：face 分片引用 gutconfig 目录，
    # acceptance 分片引用私有 .gqt-run 套件
    face = [g for g in gdirs if ".gqt-run-" not in g]
    suite = [g for g in gdirs if ".gqt-run-" in g]
    assert len(face) == 1 and len(suite) == 3
    assert "res://tests/" in face[0]


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

    def fake_gut(root, gut_args, timeout_s, env=None, gate_root=None):
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


def test_cli_canonical_end_to_end(project: Path, tmp_path: Path,
                                  monkeypatch):
    import io
    from contextlib import redirect_stdout

    from godot_qa_toolkit.cli import build_parser

    # §SPEC-075：CI 纯净环境无 godot——CLI 前置校验需可解析到存在且可
    # 执行的二进制（FakeGodot 负责 GUT 执行，不依赖真实 godot）
    monkeypatch.setenv("GQT_GODOT_BIN", sys.executable)
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


def test_flag_mode_registers_finalizer_via_hook_and_project_godot_untouched(
        project: Path, tmp_path: Path):
    """§SPEC-062A：finalizer 经 GUT pre_run_script 通道注册——GUT 调用
    窗口内 project.godot 与原文逐字节一致（autoload 表零变化）、flusher
    与 pre-hook 文件在场、argv 带 -gpre_run_script；run 结束后零残留。"""
    recipe, _ = _recipe(project, tmp_path)
    before_pg = (project / "project.godot").read_text(encoding="utf-8")
    fake = FakeGodot(project)
    sp.run = fake
    run_canonical(project, _targets(project), recipe)
    assert len(fake.pg_at_gut) == 4  # face 1 + acceptance 3 重复
    for pg_text, flusher in zip(fake.pg_at_gut, fake.flusher_at_gut):
        # 测量窗口内 project.godot 零 diff（不再注入 autoload 注册行）
        assert pg_text == before_pg
        assert flusher is True
    for argv in fake.calls:
        if "gut_cmdln.gd" in " ".join(argv):
            assert "-gpre_run_script=res://addons/gqt_cov_pre_hook.gd" in argv
    assert (project / "project.godot").read_text(
        encoding="utf-8") == before_pg
    assert not (project / "addons" / "gqt_cov_flusher.gd").exists()
    assert not (project / "addons" / "gqt_cov_pre_hook.gd").exists()


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
        r"^\s*__qa_cov_probe_m_[0-9a-f]{8}\[(\d+)\] = 1$", inst_a, re.M)
    assert sorted(map(int, hit_lines)) == list(range(len(manifest_a)))
    # 零 I/O / 零自定义函数调用 token（§SPEC-046 静态扫描）
    for token in ("FileAccess", "store_string", "store_line", "append",
                  ".close(", "__qa_cov_probe_hit(", "__qa_cov_probe_1"):
        assert token not in inst_a, f"forbidden token {token!r} in hit path"
    assert re.search(r"static var __qa_cov_probe_m_[0-9a-f]{8}: Array = "
                      r"__qa_cov_probe_reg_[0-9a-f]{8}\(\)", inst_a)
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
    assert _FLUSHER_GD.count("func _notification") == 1  # §SPEC-062A PREDELETE
    # 除 open（主+回退）与模式枚举外无任何其他 FileAccess 用法
    fa_uses = re.findall(r"FileAccess\.\w+", _FLUSHER_GD)
    assert sorted(fa_uses) == ["FileAccess.READ_WRITE", "FileAccess.WRITE",
                               "FileAccess.open", "FileAccess.open"]


def test_canonical_diagnose_shape_and_restore(project: Path,
                                              tmp_path: Path):
    """§SPEC-048：诊断模式逐 feature 输出基线/插桩两腿墙钟、wait 步数与
    归因；运行后树零残留（插桩与 finalizer 全部恢复）。"""
    from godot_qa_toolkit.canonical.orchestrator import (
        run_canonical_diagnose)

    recipe, _ = _recipe(project, tmp_path)
    fake = FakeGodot(project)
    sp.run = fake
    out = tmp_path / "diag.json"
    before = {p: p.read_bytes() for p in (project / "src_game").glob("*.gd")}
    report = run_canonical_diagnose(
        project, _targets(project), recipe, ["demo"],
        timeout_s=60, progress_out=str(out))
    assert report["report_version"] == "canonical-diagnostic/1"
    assert report["instrumentation"]["files"] == 2
    f = report["features"][0]
    assert f["key"] == "demo"
    assert f["baseline"]["wall_s"] >= 0
    assert f["instrumented"]["wall_s"] >= 0
    assert f["instrumented"]["timed_out"] is False
    assert f["wait_commands"] == 0  # demo.feature 无 wait 步骤
    assert f["attribution"]
    assert f["instrumented"]["hit_files"] >= 1
    assert out.is_file()
    for path, data in before.items():
        assert path.read_bytes() == data  # 零残留
    assert not (project / "addons" / "gqt_cov_flusher.gd").exists()
    import glob as g
    from godot_qa_toolkit.canonical.orchestrator import _backup_base
    assert g.glob(str(_backup_base() / "*")) == []


def test_flag_var_names_unique_per_file_no_inheritance_collision():
    """R5 实证缺陷回归：static var 同名在插桩的父/子类脚本间触发
    "member already exists in parent class" Parse Error，且子类探针经继承
    解析到父类数组造成索引越界。per-file 唯一变量名必须消除两者。"""
    import re as _re

    from godot_qa_toolkit.coverage.runner import _instrument

    parent_src = "extends Node\n\nfunc p() -> void:\n\tpass\n"
    child_src = "extends \"res://parent.gd\"\n\nfunc c() -> void:\n\tpass\n"
    inst_p, _ = _instrument(
        parent_src, "qa-coverage-hits-u1-aaaaaaaa.txt", flags=True)
    inst_c, _ = _instrument(
        child_src, "qa-coverage-hits-u1-bbbbbbbb.txt", flags=True)
    var_p = _re.search(r"static var (__qa_cov_probe_m_\w+): Array", inst_p)
    var_c = _re.search(r"static var (__qa_cov_probe_m_\w+): Array", inst_c)
    assert var_p and var_c
    assert var_p.group(1) != var_c.group(1), (
        "per-file static var names must differ (parent/child collision)")
    # 命中线必须引用本文件的唯一名
    assert f"{var_p.group(1)}[" in inst_p
    assert f"{var_c.group(1)}[" in inst_c


def test_flag_var_suffix_fallback_executes():
    """§SPEC-053：sink 名不含 key8 时走 hashlib fallback——该路径必须可
    执行（旧实现 hashlib 未导入，潜伏 NameError）。"""
    from godot_qa_toolkit.coverage.runner import _flag_var_suffix

    suffix = _flag_var_suffix("no-key8-sink-name.txt")
    assert len(suffix) == 8 and all(c in "0123456789abcdef" for c in suffix)


def test_canonical_crap_scores_shape_and_relative_fids(project, tmp_path):
    """§SPEC-053：canonical 输出函数级 CRAP；fid 相对路径（跨 checkout
    稳定）；分数可由 union 行集 + registry CC 重算一致。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    report = run_canonical(project, _targets(project), recipe)
    crap = report["crap"]
    assert crap["version"] == "crap-line/1"
    # §SPEC-070/R26 FR-4：record 级语义按文件测量形态区分；顶层在两种
    # 形态并存时为 mixed，单一形态时为该形态
    counts = crap["coverage_semantics_counts"]
    if counts.get("block_entry_approximation") and \
            counts.get("statement_start_line"):
        assert crap["coverage_semantics"] == "mixed"
    else:
        assert crap["coverage_semantics"] == (
            "block_entry_approximation"
            if counts["block_entry_approximation"]
            else "statement_start_line")
    recs = crap["records"]
    assert recs, "must emit function-level records"
    for r in recs:
        assert r["function_id"].startswith("src_game/"), (
            f"relative fid expected, got {r['function_id']}")
        assert not r["function_id"].startswith("/")
    scored = [r for r in recs if r["score_status"] == "scored"]
    assert scored, "happy fixture must score functions"
    for r in scored:
        cc = r["cc"]
        assert cc >= 1, f"cc must come from registry cc_oracle (>0), got {cc}"
        c = r["covered"] / r["total"]
        expect = cc * cc * (1.0 - c) ** 3 + cc
        assert r["crap_line"]["score"] == pytest.approx(expect)
        assert r["crap_line"]["score"] >= cc  # 公式下界
        assert r["crap_line"]["score_status"] == "scored"
        assert r["cov_source"] == "canonical_union"
        assert r["evidence_level"] == "exploratory"
    assert crap["summary"]["functions"] == len(recs)
    assert crap["summary"]["scored"] == len(scored)
    assert crap["comparable"] is True


def test_canonical_failure_lanes_never_score(project, tmp_path):
    """§SPEC-053：run_error 等失败样本一律不出分（score=None、covered/
    total 为空、车道具名）。"""
    recipe, _ = _recipe(project, tmp_path)
    fake = FakeGodot(project)
    fake.returncode = 1
    fake.stderr_text = "ERROR: Cannot open project"
    sp.run = fake
    report = run_canonical(project, _targets(project), recipe)
    crap = report["crap"]
    assert crap["summary"]["scored"] == 0
    assert crap["records"], "failure lanes still disclose functions"
    for r in crap["records"]:
        assert r["crap_line"]["score"] is None
        assert r["score_status"] != "scored"
        assert r["covered"] is None and r["total"] is None
    assert "run_error" in crap["summary"]["lanes"]


def test_canonical_vacuous_file_never_scores(project, tmp_path):
    (project / "src_game" / "decl.gd").write_text(
        "extends RefCounted\n\nsignal alpha\n")
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    report = run_canonical(project, _targets(project), recipe)
    decl = [r for r in report["crap"]["records"]
            if "decl.gd" not in r["function_id"]
            and r["function_id"].startswith("decl.gd")]
    # decl.gd 零可执行行 → 无 registry 函数条目；车道披露在 union 侧
    vac = [u for u in report["unions"] if "decl.gd" in u["file"]]
    assert vac and "vacuous" in vac[0]["invalid_reason"]
    for r in report["crap"]["records"]:
        assert r["crap_line"]["score"] is None or \
            r["score_status"] == "scored"


def test_canonical_subset_marks_not_comparable(project, tmp_path):
    """§SPEC-053：subset 目标集不得标 comparable。"""
    recipe, _ = _recipe(project, tmp_path)
    all_targets = _targets(project)
    subset = [all_targets[0]]
    fake = FakeGodot(project)
    sp.run = fake
    report = run_canonical(
        project, subset, recipe,
        scope_roots=[str(project / "src_game")])
    assert report["crap"]["comparable"] is False
    assert report["crap"]["comparable_reason"] == "subset_targets"
    for u in report["unions"]:
        assert u["comparable"] is False
        assert u["comparable_reason"] == "subset_targets"


def test_prep_cache_roundtrip_equivalence(project: Path, tmp_path: Path,
                                          monkeypatch):
    """§SPEC-054：第二次运行命中缓存；fresh/cache 输出核心字段逐字段
    等价（unions/crap 语义零变化）。"""
    monkeypatch.setenv("GQT_CANONICAL_PREP_DIR", str(tmp_path / "prep"))
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    first = run_canonical(project, _targets(project), recipe)
    assert first["preparation"]["cache"]["misses"] >= 2
    second = run_canonical(project, _targets(project), recipe)
    assert second["preparation"]["cache"]["hits"] >= 2
    assert second["preparation"]["cache"]["misses"] == 0

    def _semantic(rep):
        # 去除 run 级易变字段（run_uuid/wall_s）后的并集语义
        out = []
        for u in rep["unions"]:
            cu = dict(u)
            cu["arms"] = [{k: v for k, v in a.items()
                           if k not in ("run_uuid", "wall_s")}
                          for a in cu["arms"]]
            out.append(cu)
        return out

    assert _semantic(first) == _semantic(second)
    assert first["crap"]["records"] == second["crap"]["records"]
    assert first["union"] == second["union"]


def test_prep_cache_invalidates_on_source_and_recipe(project, tmp_path,
                                                     monkeypatch):
    monkeypatch.setenv("GQT_CANONICAL_PREP_DIR", str(tmp_path / "prep"))
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    run_canonical(project, _targets(project), recipe)
    # 源码变更 → 该文件 miss
    (project / "src_game" / "cell.gd").write_text(
        TARGET_SRC + "\n\nfunc extra() -> int:\n\treturn 9\n")
    r2 = run_canonical(project, _targets(project), recipe)
    assert r2["preparation"]["cache"]["misses"] >= 1
    # recipe_hash 变更 → 全 miss
    recipe2 = dict(recipe, recipe_hash="1" * 64)
    from godot_qa_toolkit.canonical.orchestrator import CanonicalError
    try:
        run_canonical(project, _targets(project), recipe2)
    except CanonicalError:
        pass  # recipe verify 可能拒绝伪造 hash —— 用缓存键层验证
    from godot_qa_toolkit.canonical import prepcache
    sha = __import__("hashlib").sha256(
        (project / "src_game" / "cell.gd").read_text(
            encoding="utf-8").encode()).hexdigest()
    assert prepcache.load_entry(
        project, rel="src_game/cell.gd", src_sha256=sha,
        recipe_hash="2" * 64) is None


def test_prep_cache_corrupt_entry_quarantined_and_rebuilt(project,
                                                          tmp_path,
                                                          monkeypatch):
    monkeypatch.setenv("GQT_CANONICAL_PREP_DIR", str(tmp_path / "prep"))
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    run_canonical(project, _targets(project), recipe)
    from godot_qa_toolkit.canonical import prepcache
    import glob as g
    entries = g.glob(str(prepcache.prep_dir(project)) + "/*.json")
    assert entries
    open(entries[0], "w").write("{corrupt")
    r = run_canonical(project, _targets(project), recipe)
    assert r["preparation"]["cache"]["misses"] >= 1
    assert g.glob(entries[0] + ".bad"), "corrupt entry must be quarantined"


def test_prep_cache_never_stores_dynamic_data(project: Path, tmp_path: Path,
                                              monkeypatch):
    """§SPEC-054：动态 hits/union/bundles 永不入缓存。"""
    import glob as g

    from godot_qa_toolkit.canonical import prepcache
    monkeypatch.setenv("GQT_CANONICAL_PREP_DIR", str(tmp_path / "prep"))
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    run_canonical(project, _targets(project), recipe)
    for path in g.glob(str(prepcache.prep_dir(project)) + "/*.json"):
        entry = json.loads(open(path, encoding="utf-8").read())
        for k in ("hits", "hits_lines", "union", "unions", "bundles",
                  "covered_line_ids"):
            assert k not in entry, f"dynamic key {k} leaked into cache"


def test_block_map_groups_straightline_runs():
    """§SPEC-055：直线段合并、控制结构/转移/缩进变化处切块。"""
    from godot_qa_toolkit.coverage.runner import (
        _blocks_from_execs, _collect_executable_lines)

    src = ("extends RefCounted\n\n\nfunc work(n: int) -> int:\n"
           "\tvar acc = 0\n\tacc += 1\n\tfor i in range(n):\n"
           "\t\tacc += i\n\tif acc > 3:\n\t\tacc = 3\n\telse:\n"
           "\t\tacc = 4\n\treturn acc\n")
    b = _blocks_from_execs(list(_collect_executable_lines(src)), src)
    assert b[5] == [5, 6]           # 直线段合并
    assert b[7] == [7]              # 控制结构自成块
    assert b[8] == [8]              # 循环体缩进变化
    assert b[13] == [13]            # return 自成块


def test_block_compression_zero_drift_vs_flags(project, tmp_path):
    """§SPEC-055 前置门：块压缩与逐语句 flags 在 fixture 上逐行零漂移。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    flags = run_canonical(project, _targets(project), recipe,
                          block_compress=False)
    blocks = run_canonical(project, _targets(project), recipe,
                           block_compress=True)

    def sem(rep):
        out = []
        for u in rep["unions"]:
            cu = dict(u)
            cu["arms"] = [{k: v for k, v in a.items()
                           if k not in ("run_uuid", "wall_s")}
                          for a in cu["arms"]]
            out.append(cu)
        return out

    assert sem(flags) == sem(blocks)
    # §SPEC-070/R26 FR-4：覆盖数据零漂移；唯一允许差异是 record 级
    # coverage_semantics 语义披露（blocks=近似 / flags=精确）
    def _strip_sem(records):
        out = []
        for r in records:
            r = dict(r)
            cl = dict(r["crap_line"])
            cl.pop("coverage_semantics")
            r.pop("coverage_semantics")
            r["crap_line"] = cl
            out.append(r)
        return out

    assert _strip_sem(flags["crap"]["records"]) == \
        _strip_sem(blocks["crap"]["records"])
    assert {r["coverage_semantics"]
            for r in blocks["crap"]["records"]} == \
        {"block_entry_approximation"}
    assert {r["coverage_semantics"]
            for r in flags["crap"]["records"]} == \
        {"statement_start_line"}
    # 块形态插桩更紧凑（同文件模板更小）
    assert (blocks["preparation"]["cache"]["algo"]
            != flags["preparation"]["cache"]["algo"])


def test_stability_baseline_routes_jitter_to_unstable_lane(project,
                                                           tmp_path):
    """§SPEC-056：跨 run 抖动 fid 必须转 unstable 车道且不出分。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    base = run_canonical(project, _targets(project), recipe)
    doc = json.loads(json.dumps(base))
    # 人为制造一个 fid 的行集漂移
    changed_fid = None
    for u in doc["unions"]:
        if u.get("union") is None:
            continue
        for row in u["union"]["per_fid"]:
            if row["covered_line_ids"]:
                row["covered_line_ids"] = row["covered_line_ids"][:-1]
                changed_fid = row["fid"]
                break
        if changed_fid:
            break
    assert changed_fid
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(doc), encoding="utf-8")
    rerun = run_canonical(project, _targets(project), recipe,
                          stability_baseline=str(baseline_path))
    lane = {r["function_id"]: r["score_status"]
            for r in rerun["crap"]["records"]}
    assert lane[changed_fid] == "unstable"
    rec = next(r for r in rerun["crap"]["records"]
               if r["function_id"] == changed_fid)
    assert rec["crap_line"]["score"] is None
    assert rerun["crap"]["stability"]["unstable_functions"] >= 1


def test_unstable_keys_captures_fid_set_changes():
    """§SPEC-057/060d 反例：基线消失/新增/行集变化/total 分母变化必须
    全部捕获（漏报缺陷回归）。"""
    from godot_qa_toolkit.canonical.orchestrator import _unstable_keys

    base = {"f.gd": {"a": (frozenset({1, 2}), 3), "b": (frozenset({3}), 2),
                     "gone": (frozenset({4}), 2),
                     "shrink": (frozenset({5}), 2)}}
    cur_unions = [
        {"file": "f.gd", "union": {"per_fid": [
            {"fid": "a", "covered_line_ids": [1], "total": 3},
            {"fid": "b", "covered_line_ids": [3], "total": 2},
            {"fid": "new", "covered_line_ids": [9], "total": 1},
            # §SPEC-060d：行集相同、分母 2→1 = 车道迁移，必须捕获
            {"fid": "shrink", "covered_line_ids": [5], "total": 1},
        ]}},
        {"file": "vanished.gd", "union": None},  # 基线有、本轮消失的文件
    ]
    keys = _unstable_keys(cur_unions, base)
    assert ("f.gd", "a") in keys          # 行集变化
    assert ("f.gd", "gone") in keys       # 基线消失
    assert ("f.gd", "new") in keys        # 新增
    assert ("f.gd", "shrink") in keys     # 分母变化（Revy H-4 反例）
    assert ("f.gd", "b") not in keys      # 行集与分母均不变 → 稳定
    # union=None 的文件由调用方按 file-vanished 处理（哨兵键另测）


def test_crap_records_mark_vanished_file_unstable(project, tmp_path):
    """§SPEC-057 反例：基线中 scored 的文件在 rerun 中 union 消失 →
    其全部函数显式 unstable 且不出分。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    all_targets = _targets(project)
    base = run_canonical(project, all_targets, recipe)
    # 基线含全部文件（cell.gd scored）；rerun 仅跑其余目标（cell.gd 本轮
    # 缺席）→ 基线 scored 函数必须回放为显式 unstable
    doc = json.loads(json.dumps(base))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(doc), encoding="utf-8")
    rerun_targets = [t for t in all_targets if "cell.gd" not in t]
    assert len(rerun_targets) == len(all_targets) - 1
    rerun = run_canonical(project, rerun_targets, recipe,
                          stability_baseline=str(baseline_path))
    lost = [r for r in rerun["crap"]["records"]
            if r["function_id"].startswith("src_game/cell.gd")]
    assert lost, "baseline-scored file must still disclose functions"
    for r in lost:
        assert r["score_status"] == "unstable", (
            f"vanishing scored file must be unstable, got {r['score_status']}")
        assert r["crap_line"]["score"] is None
    # §SPEC-060g（Revy L-1）：整文件消失按函数实数计数，__file_vanished__
    # 哨兵不得顶替函数计数（cell.gd 基线 2 个 scored 函数 → 计 2）
    assert rerun["crap"]["stability"]["unstable_functions"] == len(lost)


def test_prep_algo_report_matches_cache_key(project: Path, tmp_path: Path):
    """§SPEC-059a：报告 preparation.cache.algo 与实际缓存键算法一致——
    非块压缩路径 = flags/4-pid，块压缩 = blocks/6，不得错报。"""
    import glob as g

    from godot_qa_toolkit.canonical import prepcache
    expected = {"flags/4-pid": False, "blocks/6": True}
    for algo, bc in expected.items():
        # 清理 fixture 项目缓存目录，隔离两轮
        import shutil as sh
        prep = Path(project) / ".qa-cache" / "gqt-canonical-prep"
        if prep.is_dir():
            sh.rmtree(prep)
        recipe, _ = _recipe(project, tmp_path)
        sp.run = FakeGodot(project)
        rep = run_canonical(project, _targets(project), recipe,
                            block_compress=bc)
        assert rep["preparation"]["cache"]["algo"] == algo
        assert rep["preparation"]["cache"]["misses"] >= 1


def test_xdg_tmp_dirs_cleaned_on_sink_read_exception(project: Path,
                                                     tmp_path: Path,
                                                     monkeypatch):
    """§SPEC-059b：sink 读回抛异常时，分片 XDG 临时目录仍被清理（不残留）。"""
    import glob as g
    import tempfile as tf

    import godot_qa_toolkit.canonical.orchestrator as orch

    def boom(*a, **k):
        raise OSError("sink read exploded")

    # §SPEC-060h：实现走 tempfile.gettempdir()，断言必须同源，防自定义
    # TMPDIR 下断言空转
    pattern = str(Path(tf.gettempdir()) / "gqt-shard-*")

    monkeypatch.setattr(orch, "_read_hits_merged", boom)
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    before = set(g.glob(pattern))
    with pytest.raises(OSError):
        run_canonical(project, _targets(project), recipe)
    after = set(g.glob(pattern))
    assert after <= before, "xdg tmp dirs must be cleaned on exception"


class FailingConfigFaceGodot(FakeGodot):
    """H-1 反例注入：gut_full 臂（无 .gqt-run-）的 GUT 输出带一个普通
    测试失败（rc=1 + Run Summary 段 [Failed] 形态，非启动失败）。"""

    def __call__(self, *a, **k):
        r = super().__call__(*a, **k)
        cmd = " ".join(a[0])
        if "gut_cmdln.gd" in cmd and ".gqt-run-" not in cmd:
            r.returncode = 1
            r.stderr = ("- test_font_row_height\n"
                        "  [Failed] Expected row height 12 got 13\n")
        return r


def test_new_test_failures_gate_arm_and_disclosure(project, tmp_path):
    """§SPEC-060a 反例（Revy H-1）：普通 GUT 失败相对空基线即新增失败，
    必须门控本臂：ok=false、union invalid、无 scored、逐名披露。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FailingConfigFaceGodot(project)
    report = run_canonical(project, _targets(project), recipe)
    assert report["ok"] is False
    assert report["failures"]["gated"] is True
    assert report["failures"]["arms"]["gut_full"]["observed"] == \
        ["test_font_row_height"]
    assert report["failures"]["arms"]["gut_full"]["new"] == \
        ["test_font_row_height"]
    assert all(not u["valid"] for u in report["unions"])
    assert not any(r["score_status"] == "scored"
                   for r in report["crap"]["records"])
    assert report["crap"]["comparable"] is False
    assert report["crap"]["comparable_reason"] == "new_test_failures"


def test_baseline_failures_absorb_preexisting_failures(project, tmp_path):
    """§SPEC-060a：既有失败（基线同名）不门控——canonical 失败集 == 基线
    失败集时臂有效、评分照常（配 §SPEC-060b 插桩透明化）。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FailingConfigFaceGodot(project)
    report = run_canonical(project, _targets(project), recipe,
                           baseline_failures={("", "test_font_row_height")})
    assert report["ok"] is True
    assert report["failures"]["gated"] is False
    assert report["failures"]["arms"]["gut_full"]["new"] == []
    assert report["failures"]["arms"]["gut_full"]["observed"] == \
        ["test_font_row_height"]
    assert all(u["valid"] for u in report["unions"])
    assert any(r["score_status"] == "scored"
               for r in report["crap"]["records"])


def test_xdg_tmp_dirs_cleaned_on_worker_build_exception(project, tmp_path,
                                                        monkeypatch):
    """§SPEC-060e/063f 反例（Revy M-1/L-3）：worker 异常注入点在 XDG 创建
    **之后**（_run_one_gut 抛出），断言非空转——已创建的分片 XDG 临时目录
    仍被清理。"""
    import glob as g
    import tempfile as tf

    import godot_qa_toolkit.canonical.orchestrator as orch
    pattern = str(Path(tf.gettempdir()) / "gqt-shard-*")

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise OSError("gut worker exploded")

    monkeypatch.setattr(orch, "_run_one_gut", boom)
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    before = set(g.glob(pattern))
    with pytest.raises(OSError):
        run_canonical(project, _targets(project), recipe)
    after = set(g.glob(pattern))
    assert calls["n"] >= 1, "worker must have been invoked after XDG creation"
    assert after <= before, "xdg tmp dirs must be cleaned on worker build " \
                            "exception too"


def test_prep_cache_tampered_registry_quarantined(project, tmp_path,
                                                  monkeypatch):
    """§SPEC-060c 反例（Revy H-3）：篡改缓存内 registry 的 cc_oracle 必须
    quarantine + 回落 fresh 重建——伪造 CC 不得进入评分。"""
    import glob as g
    import json as j

    from godot_qa_toolkit.canonical import prepcache
    monkeypatch.setenv("GQT_CANONICAL_PREP_DIR", str(tmp_path / "prep"))
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    fresh = run_canonical(project, _targets(project), recipe)
    fid = next(r["function_id"] for r in fresh["crap"]["records"])
    fresh_cc = next(r["cc"] for r in fresh["crap"]["records"]
                    if r["function_id"] == fid)
    assert fresh_cc >= 1

    # 直接篡改缓存条目内的 registry 语义内容（绕过 store 接口）
    entries = g.glob(str(prepcache.prep_dir(project)) + "/*.json")
    assert entries
    for path in entries:
        doc = j.loads(Path(path).read_text(encoding="utf-8"))
        for e in doc.get("registry", {}).get("entries", []):
            e["cc_oracle"] = 99
        Path(path).write_text(j.dumps(doc), encoding="utf-8")

    warm = run_canonical(project, _targets(project), recipe)
    warm_cc = next(r["cc"] for r in warm["crap"]["records"]
                   if r["function_id"] == fid)
    assert warm_cc == fresh_cc, "tampered cached CC must never reach scoring"
    assert warm["crap"]["records"] == fresh["crap"]["records"]
    bad = g.glob(str(prepcache.prep_dir(project)) + "/*.bad")
    assert bad, "tampered entry must be quarantined as .bad"


class TwoFailuresConfigFaceGodot(FakeGodot):
    """§SPEC-061b 反例注入：gut_full 臂两个普通失败——字体族（基线内）+
    一个「插桩不容忍」契约测试（原文匹配撞探针行）。"""

    def __call__(self, *a, **k):
        r = super().__call__(*a, **k)
        cmd = " ".join(a[0])
        if "gut_cmdln.gd" in cmd and ".gqt-run-" not in cmd:
            r.returncode = 1
            r.stderr = ("- test_font_row_height\n"
                        "  [Failed] Expected row height 12 got 13\n"
                        "- test_intolerant_probe_shape\n"
                        "  [Failed] byte-exact source contract sees probe "
                        "lines\n")
        return r


def test_intolerant_declared_bypasses_gating_and_disclosed(project, tmp_path):
    """§SPEC-061b：具名声明项旁路失败门控（不门控、不静默），报告逐名
    披露理由并作为覆盖率盲点声明。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = TwoFailuresConfigFaceGodot(project)
    report = run_canonical(
        project, _targets(project), recipe,
        baseline_failures={("", "test_font_row_height")},
        intolerant_config={"tests": [{"name": "test_intolerant_probe_shape",
                                      "reason": "byte-exact source contract "
                                                "breaks under probe lines"}]})
    assert report["ok"] is True
    assert report["failures"]["gated"] is False
    assert report["failures"]["arms"]["gut_full"]["new"] == []
    assert report["failures"]["arms"]["gut_full"][
        "instrumentation_intolerant"] == ["::test_intolerant_probe_shape"]
    blk = report["instrumentation_intolerant"]
    assert blk["observed"] == ["::test_intolerant_probe_shape"]
    assert blk["declared"] == [{"name": "test_intolerant_probe_shape",
                                "reason": "byte-exact source contract breaks "
                                          "under probe lines"}]
    assert blk["coverage_blind_spots"] == ["test_intolerant_probe_shape"]
    assert blk["note"]
    assert any(r["score_status"] == "scored"
               for r in report["crap"]["records"])


def test_undeclared_intolerant_failure_still_gates(project, tmp_path):
    """§SPEC-061b 反向：未具名的插桩新增失败照常门控（旁路仅限具名项）。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = TwoFailuresConfigFaceGodot(project)
    report = run_canonical(project, _targets(project), recipe,
                           baseline_failures={("", "test_font_row_height")})
    assert report["ok"] is False
    assert report["failures"]["gated"] is True
    assert report["failures"]["arms"]["gut_full"]["new"] == \
        ["test_intolerant_probe_shape"]
    assert report["instrumentation_intolerant"]["declared"] == []
    assert not any(r["score_status"] == "scored"
                   for r in report["crap"]["records"])


def test_coverage_limitation_reason_named_on_unstable(project, tmp_path):
    """§SPEC-061c：声明的覆盖限制文件，其 unstable 记录必须携带具名理由
    （provenance.stability_reason），报告披露限制清单——不伪造确定性。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    base = run_canonical(project, _targets(project), recipe)
    doc = json.loads(json.dumps(base))
    changed_fid = None
    for u in doc["unions"]:
        if u.get("union") is None:
            continue
        for row in u["union"]["per_fid"]:
            if row["covered_line_ids"]:
                row["covered_line_ids"] = row["covered_line_ids"][:-1]
                changed_fid = row["fid"]
                break
        if changed_fid:
            break
    assert changed_fid
    rel = changed_fid.split("::")[0]
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(doc), encoding="utf-8")
    rerun = run_canonical(
        project, _targets(project), recipe,
        stability_baseline=str(baseline_path),
        intolerant_config={"coverage_limitations": [
            {"file": rel, "reason": "tool limitation: late-mounted UI "
                                    "instantiation is not reproducible "
                                    "under instrumentation slowdown"}]})
    blk = rerun["instrumentation_intolerant"]
    assert blk["declared"] == []
    assert rerun["coverage_limitations"] == [
        {"file": rel, "reason": "tool limitation: late-mounted UI "
                                "instantiation is not reproducible under "
                                "instrumentation slowdown"}]
    rec = next(r for r in rerun["crap"]["records"]
               if r["function_id"] == changed_fid)
    assert rec["score_status"] == "unstable"
    assert rec["provenance"]["stability_reason"].startswith("tool limitation")


def test_acceptance_repeats_union_convergence_disclosed(project, tmp_path):
    """§SPEC-062C：验收臂 N 次重复取行集并集，报告披露收敛轮数；
    确定性 fixture 每轮并集相同 → 第 1 轮即收敛（rounds_used=1 < N）。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    report = run_canonical(project, _targets(project), recipe,
                           acceptance_repeats=3)
    arm_b = next(a for a in report["arms"] if a["arm"] == "acceptance")
    assert arm_b["repeats"] == 3
    conv = arm_b["convergence"]
    assert conv["bound"] == 3
    assert conv["converged"] is True
    assert conv["rounds_used"] < 3
    arm_a = next(a for a in report["arms"] if a["arm"] == "gut_full")
    assert arm_a["repeats"] == 1
    assert arm_a["convergence"] is None
    # 并集语义：重复不虚增覆盖（确定性 rep 并集 == 单轮全量）
    assert all(u["valid"] for u in report["unions"])


def test_acceptance_repeats_nonconvergence_reported_with_bound(
        project, tmp_path, monkeypatch):
    """§SPEC-062C 反例：逐轮新增命中 → rounds_used == bound 且
    converged=false（未收敛如实披露，不伪造确定性）。"""
    import godot_qa_toolkit.canonical.orchestrator as orch

    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)

    real_merged = orch._read_hits_merged

    def growing_merged(root, run_uuid, rel, manifest, xdg_homes,
                       retain_dir=None, consume=True):
        hits, probes = real_merged(root, run_uuid, rel, manifest, xdg_homes,
                                   retain_dir=retain_dir, consume=consume)
        # §SPEC-063f：rep 序号只解析 XDG basename（合法 TMPDIR 含 -r15-
        # 时不得把父目录误判为 rep 轮次）
        if xdg_homes:
            base = Path(sorted(xdg_homes)[-1]).name
            m = re.search(r"(?:^|-)r(\d+)-", base)
            rep = int(m.group(1)) if m else 0
            if rep > 0 and manifest:
                extra = min(manifest[-1], max(hits) + rep) if hits else \
                    manifest[0]
                hits = set(hits) | {int(extra)}
        return hits, probes

    monkeypatch.setattr(orch, "_read_hits_merged", growing_merged)
    report = run_canonical(project, _targets(project), recipe,
                           acceptance_repeats=3)
    arm_b = next(a for a in report["arms"] if a["arm"] == "acceptance")
    conv = arm_b["convergence"]
    assert conv["bound"] == 3
    assert conv["rounds_used"] == 3
    assert conv["converged"] is False


def test_flusher_drains_in_predelete_not_exit_tree():
    """§SPEC-062A（R5 drain 顺序重验证结论）：GUT 收尾把注入节点移出树，
    _exit_tree 早于引擎关停相位会丢 autoload shutdown 命中；drain 必须挂
    NOTIFICATION_PREDELETE（孤儿节点析构晚于 autoload 退出相位，隔离实验
    实证双标记全捕获）。"""
    import godot_qa_toolkit.canonical.orchestrator as orch

    assert "NOTIFICATION_PREDELETE" in orch._FLUSHER_GD
    assert "func _exit_tree" not in orch._FLUSHER_GD


def test_timeout_cause_tail_decodes_bytes():
    """TimeoutExpired.stdout 恒为 bytes（即便 subprocess text=True）——
    超时路径 tail 拼接必须安全解码，不得 TypeError。"""
    from godot_qa_toolkit.canonical.orchestrator import _cause_tail

    class FakeTE:
        stdout = b"partial stdout"
        stderr = b"partial stderr"

    assert _cause_tail(FakeTE()) == "partial stdout\npartial stderr"
    assert _cause_tail(None) == ""


class UnparseableFaceGodot(FakeGodot):
    """§SPEC-063a 反例注入：gut_full 臂 rc=1 但输出只有通过行（无可解析
    失败名、非启动失败形态）——不可归因失败必须 fail-closed 门控。"""

    def __call__(self, *a, **k):
        r = super().__call__(*a, **k)
        cmd = " ".join(a[0])
        if "gut_cmdln.gd" in cmd and ".gqt-run-" not in cmd:
            r.returncode = 1
            r.stderr = "* test_seen\n  [Passed] all fine\n" \
                       "POST-RUN ENGINE ANOMALY: something odd happened\n"
        return r


def test_unparseable_nonzero_failure_gates_arm(project, tmp_path):
    """§SPEC-063a 反例（Revy 新 H-1）：rc=1 且无可解析失败名 = 不可归因
    失败 → run_error 门控（ok=false、union invalid、零 scored）。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = UnparseableFaceGodot(project)
    report = run_canonical(
        project, _targets(project), recipe,
        baseline_failures={("", "test_font_row_height")})
    assert report["ok"] is False
    assert report["failures"]["gated"] is False  # 非新增失败路径
    gut = next(a for a in report["arms"] if a["arm"] == "gut_full")
    assert gut["state"] == "invalid"
    assert gut["invalid_reason"] == "run_error"
    assert not any(r["score_status"] == "scored"
                   for r in report["crap"]["records"])


class ReconcileBreakerGodot(FakeGodot):
    """§SPEC-063b 反例注入：acceptance 臂输出多执行一个未声明脚本
    （全部 [Passed]）→ 对账 R2 失败。"""

    def __call__(self, *a, **k):
        r = super().__call__(*a, **k)
        cmd = " ".join(a[0])
        if "gut_cmdln.gd" in cmd and ".gqt-run-" in cmd:
            r.stdout = r.stdout + "\nres://somewhere/undeclared_extra.gd" \
                                  "\n* test_undeclared_extra"
        return r


def test_reconciliation_failure_invalidates_union_and_scores(
        project, tmp_path):
    """§SPEC-063b 反例（Revy 新 H-2）：acceptance 对账失败必须臂无效 +
    union 无效 + 零出分（不得静默 scored）。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = ReconcileBreakerGodot(project)
    report = run_canonical(project, _targets(project), recipe)
    acc = next(a for a in report["arms"] if a["arm"] == "acceptance")
    assert acc["state"] == "invalid"
    assert acc["invalid_reason"] == "reconciliation_failed"
    assert acc["reconciliation"]["ok"] is False
    assert report["ok"] is False
    assert all(not u["valid"] for u in report["unions"])
    assert not any(r["score_status"] == "scored"
                   for r in report["crap"]["records"])


class CrossScriptDuplicateGodot(FakeGodot):
    """§SPEC-063c 反例注入：gut_full 臂两个脚本同名失败 test_duplicate
    （脚本头 + `* test_duplicate` + [Failed] 双段）。"""

    def __call__(self, *a, **k):
        r = super().__call__(*a, **k)
        cmd = " ".join(a[0])
        if "gut_cmdln.gd" in cmd and ".gqt-run-" not in cmd:
            r.returncode = 1
            r.stderr = (
                "res://tests/unit/test_game.gd\n"
                "* test_duplicate\n"
                "  [Failed] script one instance\n"
                "res://tests/unit/test_other.gd\n"
                "* test_duplicate\n"
                "  [Failed] script two instance\n")
        return r


def test_same_name_cross_script_exclusion_is_identity_scoped(
        project, tmp_path):
    """§SPEC-063c 反例（Revy 新 H-3）：排除声明带 script 时仅豁免该脚本
    的失败对，另一脚本的同名失败必须照常门控（不得合并豁免）。"""
    import os as _os
    (project / "tests" / "unit" / "test_other.gd").write_text(
        "extends GutTest\nfunc test_duplicate() -> void:\n"
        "\tassert_true(false)\n")
    recipe, _ = _recipe(project, tmp_path)
    sp.run = CrossScriptDuplicateGodot(project)
    report = run_canonical(
        project, _targets(project), recipe,
        intolerant_config={"tests": [
            {"name": "test_duplicate",
             "script": "res://tests/unit/test_game.gd",
             "reason": "byte-exact contract scoped to script one"}]})
    gut = next(a for a in report["arms"] if a["arm"] == "gut_full")
    tf = gut["test_failures"]
    assert tf["instrumentation_intolerant"] == \
        ["res://tests/unit/test_game.gd::test_duplicate"]
    assert sorted(tf["new_pairs"]) == \
        ["res://tests/unit/test_other.gd::test_duplicate"]
    assert report["failures"]["gated"] is True
    assert report["ok"] is False
    assert not any(r["score_status"] == "scored"
                   for r in report["crap"]["records"])


def test_intolerant_declaration_requires_reason(project, tmp_path):
    """§SPEC-063e 反例（Revy M-2）：缺非空 reason 的声明 = 配置错误，
    fail-fast 拒绝（不得空论证旁路）。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    from godot_qa_toolkit.canonical.orchestrator import CanonicalError
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe,
                      intolerant_config={"tests": [
                          {"name": "test_inf_bookmark_and_share_signals_"
                                   "are_noops"}]})
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe,
                      intolerant_config={"tests": [
                          {"name": "test_x", "reason": "   "}]})


def test_prep_cache_non_dict_root_quarantined(project, tmp_path,
                                              monkeypatch):
    """§SPEC-063d 反例（Revy M-1）：缓存文件为合法 JSON 但根类型非 dict
    （[]）→ quarantine + miss（fresh 重建），不得 AttributeError。"""
    import glob as g
    import json as j

    from godot_qa_toolkit.canonical import prepcache
    monkeypatch.setenv("GQT_CANONICAL_PREP_DIR", str(tmp_path / "prep"))
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    run_canonical(project, _targets(project), recipe)
    entries = g.glob(str(prepcache.prep_dir(project)) + "/*.json")
    assert entries
    for path in entries:
        Path(path).write_text("[]", encoding="utf-8")
    warm = run_canonical(project, _targets(project), recipe)
    assert warm["preparation"]["cache"]["hits"] == 0, \
        "non-dict root entries must be quarantined, not loaded"
    assert g.glob(str(prepcache.prep_dir(project)) + "/*.bad"), \
        "non-dict root must be quarantined as .bad"


class MixedSummaryGodot(FakeGodot):
    """§SPEC-064a 反例注入（Revy 新 N-H-1）：gut_full 臂 rc=1，输出含
    已归因基线失败（运行流）+ Run Summary 段新失败（未归因）——混合形态
    必须门控，Summary 失败不得静默丢弃。"""

    def __call__(self, *a, **k):
        r = super().__call__(*a, **k)
        cmd = " ".join(a[0])
        if "gut_cmdln.gd" in cmd and ".gqt-run-" not in cmd:
            r.returncode = 1
            r.stderr = (
                "res://tests/unit/test_game.gd\n"
                "* test_known_baseline\n"
                "  [Failed] known pre-existing\n"
                "Run Summary\n"
                "- test_summary_secret\n"
                "  [Failed] hidden new failure\n")
        return r


def test_summary_section_failures_gate_mixed_form(project, tmp_path):
    """§SPEC-064a 反例（Revy N-H-1）：已归因基线失败 + Summary-only 新
    失败的混合形态必须门控——Summary 失败以 ("", test) 未归因对纳入。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = MixedSummaryGodot(project)
    report = run_canonical(
        project, _targets(project), recipe,
        baseline_failures={("res://tests/unit/test_game.gd",
                            "test_known_baseline")})
    gut = next(a for a in report["arms"] if a["arm"] == "gut_full")
    tf = gut["test_failures"]
    assert "::test_summary_secret" in tf["new_pairs"], tf
    assert report["failures"]["gated"] is True
    assert report["ok"] is False
    assert not any(r["score_status"] == "scored"
                   for r in report["crap"]["records"])


def test_baseline_absorption_is_pair_scoped_and_rejects_bare_names(
        project, tmp_path):
    """§SPEC-064b 反例（Revy N-M-3）：基线按 (script, test) 身份对吸收——
    脚本 A 的既有同名失败不吸收脚本 B 的新同名失败；裸名基线身份歧义
    fail-closed 拒绝。"""
    import os as _os
    (project / "tests" / "unit" / "test_other.gd").write_text(
        "extends GutTest\nfunc test_duplicate() -> void:\n"
        "\tassert_true(false)\n")
    recipe, _ = _recipe(project, tmp_path)
    sp.run = CrossScriptDuplicateGodot(project)
    report = run_canonical(
        project, _targets(project), recipe,
        baseline_failures={("res://tests/unit/test_game.gd",
                            "test_duplicate")})
    gut = next(a for a in report["arms"] if a["arm"] == "gut_full")
    tf = gut["test_failures"]
    assert sorted(tf["new_pairs"]) == \
        ["res://tests/unit/test_other.gd::test_duplicate"], tf
    assert report["failures"]["gated"] is True
    assert report["ok"] is False

    from godot_qa_toolkit.canonical.orchestrator import CanonicalError
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe,
                      baseline_failures={"test_duplicate"})


def test_coverage_limitation_requires_file_and_reason(project, tmp_path):
    """§SPEC-064c 反例（Revy N-M-1）：coverage_limitations 缺 file 或非空
    reason → CanonicalError fail-fast（盲点披露不得静默消失）。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    from godot_qa_toolkit.canonical.orchestrator import CanonicalError
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe,
                      intolerant_config={"coverage_limitations": [
                          {"file": "scenes/ui/infi/infi_post_visual.gd"}]})
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe,
                      intolerant_config={"coverage_limitations": [
                          {"file": "a.gd", "reason": "  "}]})


def test_parse_failing_pairs_summary_and_gqt_run_normalization():
    """§SPEC-064a/b 解析钉定：Summary 段 ("", test) 未归因对纳入；.gqt-run
    per-run uuid 归一为 `.gqt-run-*`（跨 run 基线可比）。"""
    from godot_qa_toolkit.canonical.orchestrator import _parse_failing_pairs
    out = (
        "res://acceptance/generated/.gqt-run-ab12cd34/generated/test_x.gd\n"
        "* test_known\n"
        "  [Failed] a\n"
        "Run Summary\n"
        "- test_secret\n"
        "  [Failed] b\n")
    pairs = _parse_failing_pairs(out)
    assert pairs == {
        ("res://acceptance/generated/.gqt-run-*/generated/test_x.gd",
         "test_known"),
        ("", "test_secret"),
    }


def test_config_fields_must_be_non_empty_strings(project, tmp_path):
    """§SPEC-065 反例（Revy R17-LOW）：tests.name/script 与
    coverage_limitations.file/reason 须为非空 str——类型错（如 file=123，
    会在附着层失配静默失效）或空值一律 CanonicalError fail-fast。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    from godot_qa_toolkit.canonical.orchestrator import CanonicalError
    cases = [
        {"tests": [{"name": 123, "reason": "r"}]},
        {"tests": [{"name": "t", "reason": "r", "script": 123}]},
        {"tests": [{"name": "t", "reason": "r", "script": ""}]},
        {"tests": ["not-a-dict"]},
        {"coverage_limitations": [{"file": 123, "reason": "r"}]},
        {"coverage_limitations": [{"file": "a.gd", "reason": ""}]},
        {"coverage_limitations": ["not-a-dict"]},
    ]
    for cfg in cases:
        with pytest.raises(CanonicalError):
            run_canonical(project, _targets(project), recipe,
                          intolerant_config=cfg)


def test_valid_config_passes_type_validation(project, tmp_path):
    """§SPEC-065 正向：合法声明（含 script 作用域 + coverage limitation）
    通过类型校验，报告 declared/limitations 逐字段保持非空 str。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    report = run_canonical(
        project, _targets(project), recipe,
        intolerant_config={
            "tests": [{"name": "test_t", "reason": "contract",
                       "script": "res://tests/unit/test_game.gd"}],
            "coverage_limitations": [
                {"file": "src_game/cell.gd", "reason": "tool limitation"}]})
    d = report["instrumentation_intolerant"]["declared"]
    assert d == [{"name": "test_t", "reason": "contract",
                  "script": "res://tests/unit/test_game.gd"}]
    assert report["coverage_limitations"] == [
        {"file": "src_game/cell.gd", "reason": "tool limitation"}]
    assert report["ok"] is True


def test_script_null_rejected_and_absent_script_legal(project, tmp_path):
    """§SPEC-065/R19 反例（Revy LOW）：显式 `script:null` 不得静默降级为
    bare-name 声明——键提供即须通过非空 str 校验（CanonicalError）；
    键缺省保留既有 bare-name 合法行为（declared 无 script 键）。"""
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    from godot_qa_toolkit.canonical.orchestrator import CanonicalError
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe,
                      intolerant_config={"tests": [
                          {"name": "test_x", "script": None,
                           "reason": "reason"}]})
    # 同类可选顶层键：显式 null / 非 list 拒绝（不得静默当空）
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe,
                      intolerant_config={"tests": None})
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe,
                      intolerant_config={"coverage_limitations": None})
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe,
                      intolerant_config={"coverage_limitations": "x"})
    # 键缺省（bare-name）合法路径不变：declared 无 script 键
    report = run_canonical(
        project, _targets(project), recipe,
        intolerant_config={"tests": [{"name": "test_t",
                                      "reason": "contract"}]})
    d = report["instrumentation_intolerant"]["declared"]
    assert d == [{"name": "test_t", "reason": "contract"}]
    assert all("script" not in x for x in d)


def test_spec066_recipe_illegal_shapes_domain_errors(project, tmp_path,
                                                     monkeypatch):
    """§SPEC-066/R20（Revy R19-L1）：合法 JSON 非 dict recipe 根与
    exclusions 直接消费结构（list/行/reason）非法形状必须转领域错误——
    API 侧 verify_recipe not-ok / build_recipe (None, reason) /
    run_canonical CanonicalError；真实 CLI（canonical 与 canonical-diagnose）
    输出 tool JSON ok=false，不得泄漏 AttributeError/TypeError traceback；
    合法 exclusions 行为不变。§SPEC-075：CLI 前置 godot 校验需环境可解析。"""
    import io
    from contextlib import redirect_stdout

    from godot_qa_toolkit.canonical.recipe import (
        RECIPE_VERSION,
        build_recipe,
        verify_recipe,
    )
    from godot_qa_toolkit.cli import build_parser

    # §SPEC-075：CI 纯净环境无 godot——CLI 前置校验需可解析到存在且
    # 可执行的二进制（sys.executable 恒满足；FakeGodot 负责 GUT 执行）
    monkeypatch.setenv("GQT_GODOT_BIN", sys.executable)

    # API：verify_recipe 非 dict 根 → not-ok + reason（原 AttributeError）
    for bad_root in ([1, 2], "recipe", 7, None):
        ok, reason = verify_recipe(bad_root)
        assert ok is False
        assert "must be a JSON object" in reason, reason

    # API：exclusions 直接消费结构逐项
    ok, reason = verify_recipe({"recipe_version": RECIPE_VERSION,
                                "exclusions": {"a": 1}, "recipe_hash": "x"})
    assert ok is False and "must be a list" in reason
    ok, reason = verify_recipe({"recipe_version": RECIPE_VERSION,
                                "exclusions": ["x"], "recipe_hash": "x"})
    assert ok is False and "must be an object" in reason
    ok, reason = verify_recipe({"recipe_version": RECIPE_VERSION,
                                "exclusions": [7], "recipe_hash": "x"})
    assert ok is False and "must be an object" in reason
    ok, reason = verify_recipe(
        {"recipe_version": RECIPE_VERSION,
         "exclusions": [{"scope": "gut_dir", "entry": "x", "reason": 123}],
         "recipe_hash": "x"})
    assert ok is False and "must be a non-empty string" in reason
    # API-only 形态：非 JSON 可序列化值 → 领域错误，不泄漏 TypeError
    ok, reason = verify_recipe({"recipe_version": RECIPE_VERSION,
                                "exclusions": [], "recipe_hash": "x",
                                "junk": {1, 2}})
    assert ok is False and "serializable" in reason

    # API：run_canonical 携带非法 recipe → CanonicalError（统一领域出口）
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), [1, 2])

    # API：build_recipe exclusions 非法形状 → (None, reason)
    for bad in ({"scope": "x"}, ["x"], "",
                [{"scope": ["gut_dir"], "entry": "x", "reason": "r"}],
                [{"scope": "gut_dir", "entry": 7, "reason": "r"}],
                [{"scope": "gut_dir", "entry": "x", "reason": 123}]):
        rec, err = build_recipe(project, exclusions=bad)
        assert rec is None and err, bad

    # 合法正例：exclusions 合法时行为不变
    rec, err = build_recipe(project, exclusions=[
        {"scope": "gut_dir", "entry": "res://tests/old/",
         "reason": "retired face"}])
    assert err is None
    ok, reason = verify_recipe(rec)
    assert ok is True, reason

    # 真实 CLI：canonical 非 dict recipe 根 → exit 1 + tool JSON ok=false
    # （stdout 整体可解析为 tool JSON 本身即证明无 traceback）
    bad = tmp_path / "bad_recipe.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    out_path = tmp_path / "out.json"
    parser = build_parser()
    args = parser.parse_args([
        "canonical", str(project / "src_game"),
        "--project-root", str(project),
        "--recipe", str(bad), "--out", str(out_path)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    assert code == 1
    doc = json.loads(buf.getvalue())
    assert doc["tool"] == "canonical" and doc["ok"] is False
    assert "must be a JSON object" in doc["summary"]["error"]

    # 真实 CLI：canonical-diagnose 同拒
    args = parser.parse_args([
        "canonical-diagnose", str(project / "src_game"),
        "--project-root", str(project),
        "--recipe", str(bad), "--features", "demo",
        "--out", str(out_path)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    assert code == 1
    doc = json.loads(buf.getvalue())
    assert doc["tool"] == "canonical-diagnose" and doc["ok"] is False
    assert "must be a JSON object" in doc["summary"]["error"]


def test_spec066_stability_baseline_illegal_shapes_domain_errors(
        project, tmp_path, monkeypatch):
    """§SPEC-066/R20（Revy R19-L2）：stability baseline 合法 JSON 非 dict
    根与 unions/per_fid/crap.records 直接消费结构非法形状必须转
    CanonicalError；真实 CLI 输出 tool JSON ok=false canonical_error:true，
    不得泄漏 AttributeError/TypeError/KeyError；含 union=null 行、
    total=null、标量行集的合法基线路径不变。§SPEC-075：CLI 前置 godot
    校验需环境可解析。"""
    import io
    from contextlib import redirect_stdout

    from godot_qa_toolkit.canonical.orchestrator import _load_baseline_report
    from godot_qa_toolkit.cli import build_parser

    # §SPEC-075：CI 纯净环境无 godot——CLI 前置校验需可解析到存在且
    # 可执行的二进制（sys.executable 恒满足；FakeGodot 负责 GUT 执行）
    monkeypatch.setenv("GQT_GODOT_BIN", sys.executable)
    recipe, rpath = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)

    def _baseline_err(doc) -> str:
        p = tmp_path / "baseline.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(CanonicalError) as ei:
            run_canonical(project, _targets(project), recipe,
                          stability_baseline=str(p))
        return str(ei.value)

    # 根非 dict（R19-L2 原反例）
    assert "must be a JSON object" in _baseline_err([1, 2])
    # unions 直接消费结构逐项
    assert "must be a list" in _baseline_err({"unions": "x"})
    assert "must be an object" in _baseline_err({"unions": ["x"]})
    assert "must be a non-empty string" in _baseline_err(
        {"unions": [{"file": 7, "union": {"per_fid": []}}]})
    assert "must be an object or null" in _baseline_err(
        {"unions": [{"file": "f.gd", "union": 7}]})
    assert "'per_fid' must be a list" in _baseline_err(
        {"unions": [{"file": "f.gd", "union": {"per_fid": "x"}}]})
    assert "must be an object" in _baseline_err(
        {"unions": [{"file": "f.gd", "union": {"per_fid": ["x"]}}]})
    assert "'fid' must be a non-empty string" in _baseline_err(
        {"unions": [{"file": "f.gd",
                     "union": {"per_fid": [{"covered_line_ids": [1]}]}}]})
    assert "'covered_line_ids' must be a list" in _baseline_err(
        {"unions": [{"file": "f.gd",
                     "union": {"per_fid": [{"fid": "a"}]}}]})
    assert "'covered_line_ids' must be a list" in _baseline_err(
        {"unions": [{"file": "f.gd",
                     "union": {"per_fid": [{"fid": "a",
                                            "covered_line_ids": "01"}]}}]})
    assert "scalars" in _baseline_err(
        {"unions": [{"file": "f.gd",
                     "union": {"per_fid": [{"fid": "a",
                                            "covered_line_ids": [1,
                                                                 {"x": 1}]}]}}]})
    assert "'total' must be a non-negative finite integer value or null" \
        in _baseline_err(
        {"unions": [{"file": "f.gd",
                     "union": {"per_fid": [{"fid": "a",
                                            "covered_line_ids": [1],
                                            "total": "abc"}]}}]})
    # crap.records 直接消费结构
    assert "'crap' must be an object" in _baseline_err({"crap": 7})
    assert "must be a list" in _baseline_err({"crap": {"records": 7}})
    assert "must be an object" in _baseline_err({"crap": {"records": ["x"]}})
    assert "'function_id' must be a non-empty string" in _baseline_err(
        {"crap": {"records": [{"score": 1}]}})

    # 真实 CLI：非 dict 根 baseline → exit 1 + tool JSON ok=false
    # canonical_error:true（CanonicalError 在 CLI 边界统一转 tool JSON）
    bad = tmp_path / "bad_baseline.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    out_path = tmp_path / "out.json"
    args = build_parser().parse_args([
        "canonical", str(project / "src_game"),
        "--project-root", str(project),
        "--recipe", str(rpath),
        "--stability-baseline", str(bad), "--out", str(out_path)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    assert code == 1
    doc = json.loads(buf.getvalue())
    assert doc["tool"] == "canonical" and doc["ok"] is False
    assert doc["summary"]["canonical_error"] is True
    assert "must be a JSON object" in doc["summary"]["error"]

    # 合法正例：union=null 行跳过、total=null → 0、str/int 标量行集可读回
    good = tmp_path / "good_baseline.json"
    good.write_text(json.dumps({
        "unions": [
            {"file": "res://a.gd",
             "union": {"per_fid": [
                 {"fid": "f", "covered_line_ids": ["0", "1"], "total": 3},
                 {"fid": "g", "covered_line_ids": [4], "total": None}]}},
            {"file": "res://gone.gd", "union": None},
        ],
        "crap": {"records": [{"function_id": "res://a.gd::f", "score": 5}]},
    }), encoding="utf-8")
    lines, records = _load_baseline_report(str(good))
    assert lines["res://a.gd"]["f"] == (frozenset({"0", "1"}), 3)
    assert lines["res://a.gd"]["g"] == (frozenset({4}), 0)
    assert "res://gone.gd" not in lines
    assert records["res://a.gd::f"]["score"] == 5
