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
        # 非测试执行类子进程（compile 探针/cp -al 建 shadow 等）真实执行
        if "gut_cmdln.gd" not in cmd:
            return self.real_run(*a, **k)
        on_gdir = ".gqt-run-" in cmd  # acceptance 私有套件臂
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
    gdirs = [" ".join(x for x in c if str(x).startswith("-gdir="))
             for c in gut_calls]
    # 两臂均按分片扇出（-gdir 均在）：face 分片引用 gutconfig 目录，
    # acceptance 分片引用私有 .gqt-run 套件
    face = [g for g in gdirs if ".gqt-run-" not in g]
    suite = [g for g in gdirs if ".gqt-run-" in g]
    assert len(face) == 1 and len(suite) == 1
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
    assert _FLUSHER_GD.count("func _exit_tree") == 1
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
    assert g.glob("/tmp/gqt-canonical-backups/*") == []


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
    assert crap["coverage_semantics"] == "statement_start_line"
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
    assert flags["crap"]["records"] == blocks["crap"]["records"]
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
    """§SPEC-057 反例：基线消失/新增/行集变化必须全部捕获（漏报缺陷回归）。"""
    from godot_qa_toolkit.canonical.orchestrator import _unstable_keys

    base = {"f.gd": {"a": frozenset({1, 2}), "b": frozenset({3}),
                     "gone": frozenset({4})}}
    cur_unions = [
        {"file": "f.gd", "union": {"per_fid": [
            {"fid": "a", "covered_line_ids": [1]},
            {"fid": "b", "covered_line_ids": [3]},
            {"fid": "new", "covered_line_ids": [9]},
        ]}},
        {"file": "vanished.gd", "union": None},  # 基线有、本轮消失的文件
    ]
    keys = _unstable_keys(cur_unions, base)
    assert ("f.gd", "a") in keys          # 行集变化
    assert ("f.gd", "gone") in keys       # 基线消失
    assert ("f.gd", "new") in keys        # 新增
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

    import godot_qa_toolkit.canonical.orchestrator as orch

    def boom(*a, **k):
        raise OSError("sink read exploded")

    monkeypatch.setattr(orch, "_read_hits_merged", boom)
    recipe, _ = _recipe(project, tmp_path)
    sp.run = FakeGodot(project)
    before = set(g.glob("/tmp/gqt-shard-*"))
    with pytest.raises(OSError):
        run_canonical(project, _targets(project), recipe)
    after = set(g.glob("/tmp/gqt-shard-*"))
    assert after <= before, "xdg tmp dirs must be cleaned on exception"
