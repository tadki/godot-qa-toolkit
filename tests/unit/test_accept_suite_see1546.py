"""SEE-1546 WP3 PR-B — accept 私有目录管线单测（§SPEC-035）。

判据先行（TDD RED）：compile --out-dir/--keys、run --suite-dir、私有目录
`acceptance/generated/.gqt-run-<uuid>/`（事先为空、跑完清理零残留）、
acceptance-suite/1 对账五项（生成数==选中数、执行脚本集==生成词干集、
计数相等、未选/旧 generated 缺席、私有目录重放语义）、W4 成本探针记录。
反向探针：植入 test_extra.gd → 对账必须失败。缺省行为不变单独钉住。
"""

from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path

import pytest

from godot_qa_toolkit.accept.compile import compile_feature
from godot_qa_toolkit.accept.run import AcceptRunError, replay_check
from godot_qa_toolkit.accept.suite import (
    SUITES_VERSION,
    SuiteError,
    compile_keys_to_suite,
    create_suite_dir,
    run_acceptance_probe,
    run_suite_dir,
)

FEATURE = """Feature: F
  Scenario Outline: s
    Then hp is <left>
    # types: <hp>:int, <left>:numeric
    Examples:
      | hp | left |
      | 10 | 7    |
"""


@pytest.fixture(autouse=True)
def _restore_subprocess():
    """fake 直接换全局 subprocess.run——autouse 兜底还原防泄漏。"""
    orig_run = sp.run
    yield
    sp.run = orig_run


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    feats = root / "acceptance" / "features"
    feats.mkdir(parents=True)
    for name in ("demo", "demo_x", "a", "ab"):
        (feats / f"{name}.feature").write_text(FEATURE, encoding="utf-8")
    return root


def _fake_gut(output: str, returncode: int = 0, capture: list | None = None):
    def fake_run(*a, **k):
        if capture is not None:
            capture.append(a[0])

        class R:
            def __init__(self):
                self.returncode = returncode
                self.stdout = output
                self.stderr = ""

        return R()

    sp.run = fake_run


def _compile_keys(project: Path, keys: list[str], suite_dir: Path) -> dict:
    rec = compile_keys_to_suite(project, keys, suite_dir)
    assert rec["keys"] == sorted(set(keys))
    return rec


def _gut_ok_output(rec: dict) -> str:
    """真机 GUT 9.6 运行流形态（lab 实跑锚定）：脚本头 = 整行 res://….gd，
    段内 `* test_x`。同名测试跨脚本靠段区分。"""
    sections = []
    for key, man in sorted(rec["manifests"].items()):
        lines = [f"res://acceptance/generated/.gqt-run-x/generated/"
                 f"test_{key}.gd"]
        for a in man["attribution"]:
            lines.append(f"* {a['test_name']}")
            lines.append("  [Passed]")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


def _main_artifacts(root: Path) -> list[str]:
    ir = root / "acceptance" / "ir"
    gen = root / "acceptance" / "generated"
    out = []
    for d in (ir, gen):
        if d.is_dir():
            out += [str(p) for p in d.glob("*") if p.is_file()]
    return sorted(out)


# ------------------------------------------------------------------
# 私有目录生命周期：事先为空 / 跑完清理零残留
# ------------------------------------------------------------------


def test_create_suite_dir_fresh_empty_under_generated(project: Path):
    d = create_suite_dir(project)
    assert d.is_dir()
    assert d.parent == project / "acceptance" / "generated"
    assert d.name.startswith(".gqt-run-")
    assert list(d.iterdir()) == []  # 事先为空


def test_create_suite_dir_refuses_existing_path(project: Path):
    d = create_suite_dir(project, run_uuid="fixed")
    assert d.is_dir()
    with pytest.raises(SuiteError, match="already exists"):
        create_suite_dir(project, run_uuid="fixed")


def test_run_suite_dir_cleans_up_zero_residue(project: Path):
    rec = _compile_keys(project, ["demo"], create_suite_dir(project))
    capture: list = []
    _fake_gut(_gut_ok_output(rec), capture=capture)
    record = run_suite_dir(project, Path(rec["suite_dir"]))
    assert record["ok"] is True
    assert record["cleanup"] == "removed"
    gen_root = project / "acceptance" / "generated"
    assert not list(gen_root.glob(".gqt-run-*"))  # 零残留
    # 私有目录里的文件不落主目录（main 只读不碰）
    assert _main_artifacts(project) == []


# ------------------------------------------------------------------
# compile --out-dir/--keys：目录即选择集（gselect 子串碰撞绕开）
# ------------------------------------------------------------------


def test_compile_keys_writes_only_selected_into_private_dir(project: Path):
    d = create_suite_dir(project)
    rec = _compile_keys(project, ["demo"], d)
    gen = d / "generated"
    ir = d / "ir"
    assert sorted(p.name for p in gen.glob("test_*.gd")) == ["test_demo.gd"]
    assert sorted(p.name for p in ir.glob("*.json")) == ["demo.json"]
    assert (gen / "demo.manifest.json").is_file()
    assert (gen / "accept_worker.gd").is_file()
    # 主目录零写入
    assert _main_artifacts(project) == []


def test_compile_keys_collision_pair_a_vs_ab_isolated(project: Path):
    """gselect 子串碰撞：主目录选 {a} 会误吸 test_ab.gd；私有目录即选择
    集——只编 a → 目录里只有 test_a.gd，全目录执行恰好是 a。"""
    d = create_suite_dir(project)
    rec = _compile_keys(project, ["a"], d)
    assert [p.name for p in (d / "generated").glob("test_*.gd")] == \
        ["test_a.gd"]
    assert "ab" not in rec["keys"]
    capture: list = []
    _fake_gut(_gut_ok_output(rec), capture=capture)
    record = run_suite_dir(project, d)
    assert record["ok"] is True
    assert record["keys"] == ["a"]
    gdir = [a for a in capture[0] if a.startswith("-gdir=")][0]
    assert ".gqt-run-" in gdir and gdir.endswith("/generated")


def test_compile_keys_unknown_key_is_suite_error(project: Path):
    d = create_suite_dir(project)
    with pytest.raises(SuiteError, match="unknown key"):
        compile_keys_to_suite(project, ["nope"], d)


# ------------------------------------------------------------------
# 对账五项（acceptance-suite/1）
# ------------------------------------------------------------------


def test_run_suite_dir_happy_all_five_rules(project: Path):
    rec = _compile_keys(project, ["demo", "demo_x"],
                        create_suite_dir(project))
    capture: list = []
    _fake_gut(_gut_ok_output(rec), capture=capture)
    record = run_suite_dir(project, Path(rec["suite_dir"]))
    assert record["ok"] is True
    assert record["suite_version"] == SUITES_VERSION
    rules = record["rules"]
    assert rules["generated_eq_selected"] is True      # 生成数==选中数
    assert rules["executed_scripts_eq_generated"] is True  # 脚本集==词干集
    assert rules["executed_count_eq_expected"] is True     # 计数相等
    assert rules["unselected_absent"] is True              # 未选/旧缺席
    assert rules["replay_checked"] is True                 # 私有目录重放
    assert record["executed_tests"]
    assert record["keys"] == ["demo", "demo_x"]


def test_run_suite_dir_gdir_points_at_private_dir(project: Path):
    rec = _compile_keys(project, ["demo"], create_suite_dir(project))
    capture: list = []
    _fake_gut(_gut_ok_output(rec), capture=capture)
    run_suite_dir(project, Path(rec["suite_dir"]))
    gdir = [a for a in capture[0] if a.startswith("-gdir=")][0]
    assert ".gqt-run-" in gdir
    assert "acceptance/generated" in gdir


def test_reverse_probe_extra_script_fails_reconcile(project: Path):
    """反向探针：植入 test_extra.gd 且 GUT 输出含其测试 → 对账失败、
    私有目录保留（不清理）、主目录零碰。"""
    rec = _compile_keys(project, ["demo"], create_suite_dir(project))
    d = Path(rec["suite_dir"])
    (d / "generated" / "test_extra.gd").write_text("# planted\n",
                                                   encoding="utf-8")
    extra = ("res://acceptance/generated/.gqt-run-x/generated/"
             "test_extra.gd\n* test_extra_thing\n  [Passed]\n")
    out = _gut_ok_output(rec) + "\n" + extra
    _fake_gut(out)
    record = run_suite_dir(project, d)
    assert record["ok"] is False
    rules = record["rules"]
    assert rules["generated_eq_selected"] is False
    # tidy L1（§SPEC-035 R4 语义收窄）：杂散文件对账归 R1 文件集管道——
    # R4 只管主目录快照一致性（X4 守卫用例保持绿即证）
    assert rules["unselected_absent"] is True
    assert rules["executed_scripts_eq_generated"] is False
    assert record["cleanup"] == "kept-on-failure"
    assert d.is_dir()  # 失败保留现场
    assert _main_artifacts(project) == []


def test_reverse_probe_extra_file_alone_fails_file_set(project: Path):
    rec = _compile_keys(project, ["demo"], create_suite_dir(project))
    d = Path(rec["suite_dir"])
    (d / "generated" / "test_extra.gd").write_text("# planted\n",
                                                   encoding="utf-8")
    _fake_gut(_gut_ok_output(rec))  # 输出只含已编测试
    record = run_suite_dir(project, d)
    assert record["ok"] is False
    assert record["rules"]["generated_eq_selected"] is False


def test_missing_executed_test_fails_count_rule(project: Path):
    rec = _compile_keys(project, ["demo", "demo_x"],
                        create_suite_dir(project))
    # demo_x 的脚本段整体缺席（GUT 只跑了 demo）
    man = rec["manifests"]["demo"]
    lines = ["res://acceptance/generated/.gqt-run-x/generated/test_demo.gd"]
    lines += [f"* {a['test_name']}" for a in man["attribution"]]
    _fake_gut("\n".join(lines) + "\n")
    record = run_suite_dir(project, Path(rec["suite_dir"]))
    assert record["ok"] is False
    assert record["rules"]["executed_count_eq_expected"] is False
    assert record["rules"]["executed_scripts_eq_generated"] is False


def test_same_scenario_name_across_features_no_collapse(project: Path):
    """跨 feature 同名场景 → 生成测试名相同（真机风险案例）：脚本段解析
    按 .gd 段归属，计数不塌缩，对账仍全过。"""
    rec = _compile_keys(project, ["demo", "demo_x"],
                        create_suite_dir(project))
    assert (rec["manifests"]["demo"]["attribution"][0]["test_name"]
            == rec["manifests"]["demo_x"]["attribution"][0]["test_name"])
    _fake_gut(_gut_ok_output(rec))
    record = run_suite_dir(project, Path(rec["suite_dir"]))
    assert record["ok"] is True
    assert record["rules"]["executed_count_eq_expected"] is True


def test_tampered_private_generated_gd_is_run_error(project: Path):
    """R5 重放门：私有目录内生成 .gd 被手改 → run_error（先于 GUT）。"""
    rec = _compile_keys(project, ["demo"], create_suite_dir(project))
    d = Path(rec["suite_dir"])
    gd = d / "generated" / "test_demo.gd"
    gd.write_text(gd.read_text(encoding="utf-8") + "\n# tampered\n",
                  encoding="utf-8")
    called: list = []
    _fake_gut("nothing", capture=called)
    with pytest.raises(AcceptRunError):
        run_suite_dir(project, d)
    assert called == []  # 重放失败绝不进 GUT


def test_replay_check_suite_dir_variant_reads_private_manifests(
        project: Path):
    d = create_suite_dir(project)
    rec = _compile_keys(project, ["demo"], d)
    man = replay_check(project, "demo", suite_dir=d)
    assert man["ir_hash"] == rec["manifests"]["demo"]["ir_hash"]
    assert man["ir_path"].startswith(
        "res://acceptance/generated/.gqt-run-")


def test_gut_lifecycle_hooks_not_counted_as_tests(project: Path):
    """真机锚定（KOL aidomain 探针）：GUT 运行流把 `* before_all` 等钩子
    打进脚本段——钩子不是测试，对账计数必须排除，否则 R3 假红。"""
    rec = _compile_keys(project, ["demo"], create_suite_dir(project))
    man = rec["manifests"]["demo"]
    lines = ["res://acceptance/generated/.gqt-run-x/generated/test_demo.gd",
             "* before_all"]
    lines += [f"* {a['test_name']}" for a in man["attribution"]]
    _fake_gut("\n".join(lines) + "\n")
    record = run_suite_dir(project, Path(rec["suite_dir"]))
    assert record["ok"] is True
    assert record["rules"]["executed_count_eq_expected"] is True


def test_gut_orphan_report_lines_not_counted_as_tests(project: Path):
    """真机锚定（KOL 全量 36 特性）：GUT 把孤儿节点报告也打成 `*` 行
    （`* [Class:<Node#id>(script)]`，单特性可注水数百条）——非 test_
    前缀条目不是测试，对账必须排除，否则 R3 假红、臂被误判 invalid。"""
    rec = _compile_keys(project, ["demo"], create_suite_dir(project))
    man = rec["manifests"]["demo"]
    lines = ["res://acceptance/generated/.gqt-run-x/generated/test_demo.gd",
             "* [LikeHeartComputer:<Node2D#1355984946042>(likeheart_computer.gd)]",
             "* [DecorMode:<CanvasItem#42>(decor_mode.gd)]"]
    lines += [f"* {a['test_name']}" for a in man["attribution"]]
    _fake_gut("\n".join(lines) + "\n")
    record = run_suite_dir(project, Path(rec["suite_dir"]))
    assert record["ok"] is True
    assert record["rules"]["executed_count_eq_expected"] is True
    assert record["unknown_executed"] == []


# ------------------------------------------------------------------
# W4 acceptance 成本探针
# ------------------------------------------------------------------


def test_probe_record_shape_and_cleanup(project: Path):
    # write=False 拿 manifest 构造真机形态 fake 输出（探针自建私有目录，
    # 预先落盘编译会留孤儿 .gqt-run-* 污染零残留断言）
    f = project / "acceptance" / "features" / "demo.feature"
    suite, error = compile_feature(f, project, write=False)
    assert error is None
    _fake_gut(_gut_ok_output({"manifests": {"demo": suite.manifest}}))
    probe = run_acceptance_probe(project, ["demo"])
    assert probe["suite_ok"] is True
    assert probe["keys"] == ["demo"]
    assert probe["compile_s"] >= 0 and probe["run_s"] >= 0
    assert probe["extrapolation_assumption"]
    gen_root = project / "acceptance" / "generated"
    assert not list(gen_root.glob(".gqt-run-*"))


# ------------------------------------------------------------------
# 缺省行为不变
# ------------------------------------------------------------------


def test_default_compile_without_out_dir_unchanged(project: Path):
    f = project / "acceptance" / "features" / "demo.feature"
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    assert (project / "acceptance" / "ir" / "demo.json").is_file()
    assert (project / "acceptance" / "generated"
            / "test_demo.gd").is_file()
    assert (project / "acceptance" / "generated"
            / "demo.manifest.json").is_file()


def test_default_replay_check_main_dirs_unchanged(project: Path):
    f = project / "acceptance" / "features" / "demo.feature"
    suite, error = compile_feature(f, project, write=True)
    assert error is None
    man = replay_check(project, "demo")
    assert man["feature_key" if "feature_key" in man else "feature"]
    assert man["ir_path"] == "res://acceptance/ir/demo.json"


# ------------------------------------------------------------------
# CLI 判据（gqt accept compile --keys / run --suite-dir / 探针缺省键）
# ------------------------------------------------------------------


def _run_cli(argv: list[str]) -> tuple[int, dict]:
    import io
    import json as j
    from contextlib import redirect_stdout

    from godot_qa_toolkit.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    return code, (j.loads(buf.getvalue()) if buf.getvalue().strip() else {})


def test_cli_compile_keys_creates_private_suite(project: Path):
    rec = None
    _fake_gut("")  # compile 阶段不跑 GUT；占位防误真机
    code, out = _run_cli([
        "accept", "compile", "--project-root", str(project),
        "--keys", "demo,demo_x",
    ])
    assert code == 0 and out["ok"] is True
    s = out["summary"]
    assert s["mode"] == "private-suite"
    assert s["keys"] == ["demo", "demo_x"]
    d = Path(s["suite_dir"])
    assert d.is_dir()
    assert sorted(p.name for p in (d / "generated").glob("test_*.gd")) == \
        ["test_demo.gd", "test_demo_x.gd"]
    assert _main_artifacts(project) == []


def test_cli_run_suite_dir_happy_and_exit_code(project: Path):
    _fake_gut("")
    code, out = _run_cli([
        "accept", "compile", "--project-root", str(project),
        "--keys", "demo",
    ])
    assert code == 0
    d = Path(out["summary"]["suite_dir"])
    rec = compile_keys_to_suite(project, ["demo"], d)
    _fake_gut(_gut_ok_output(rec))
    code, out = _run_cli([
        "accept", "run", "--project-root", str(project),
        "--suite-dir", str(d),
    ])
    assert code == 0 and out["ok"] is True
    assert out["summary"]["rules"]["replay_checked"] is True
    assert out["summary"]["cleanup"] == "removed"
    assert not list((project / "acceptance" / "generated")
                    .glob(".gqt-run-*"))


def test_cli_probe_default_key_is_aidomain_integration(project: Path):
    _fake_gut("")
    code, out = _run_cli([
        "accept", "run", "--project-root", str(project),
        "--acceptance-probe-keys",
    ])
    assert code == 1 and out["ok"] is False  # 该键在 fixture 不存在
    assert "aidomain_integration" in out["summary"]["error"]


def test_cli_run_without_key_or_suitdir_errors(project: Path):
    code, out = _run_cli(["accept", "run", "--project-root", str(project)])
    assert code == 1 and out["ok"] is False
    assert "--key" in out["summary"]["error"]
