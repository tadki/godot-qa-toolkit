"""SEE-1605 §SPEC-002：首触 auto map 失败路径的具名 fail-loud。

判据：导入失败 / 导入产物不全 / 测量失败（应出产物未落盘）/ arms 无效时，
直接生成路径与 `measure risk` CLI 均具名失败——不得静默继续、不得输出假
`ok:true`（CLI 非零退出 + `failures` 具名原因）、不得留下可被消费的
半成品（默认 map 位置无产物）。`--skip-measure` 静态 dry run 不受影响。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import truth_select as ts
from godot_qa_toolkit.mutation.truth_select import (MutationSelectionError,
                                                    ensure_map)

SCHEMA = "test-attribution/7"

ELIGIBLE_A = "func a():\n\treturn 1\n"
ELIGIBLE_B = "func b():\n\treturn 2\n"

MAP_REL = ".qa-cache/attribution-map.json"


def _mk_virgin(root: Path) -> Path:
    (root / "autoload").mkdir(parents=True)
    (root / "systems" / "economy").mkdir(parents=True)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="virgin-fixture"\n',
        encoding="utf-8")
    (root / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/"], "include_subdirs": True,
         "prefix": "test_"}), encoding="utf-8")
    (root / "autoload" / "economy_manager.gd").write_text(
        ELIGIBLE_A, encoding="utf-8")
    (root / "systems" / "economy" / "lcps_calculator.gd").write_text(
        ELIGIBLE_B, encoding="utf-8")
    (root / "tests" / "unit" / "test_game.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tassert_true(true)\n",
        encoding="utf-8")
    feats = root / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(
        "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
        "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
        "      | hp | left |\n      | 10 | 7    |\n", encoding="utf-8")
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(root)

    return root


def _sha(root: Path, rel: str) -> str:
    return hashlib.sha256(
        (Path(root) / rel).read_bytes()).hexdigest()


def _fresh_doc(root: Path, targets: list[str]) -> dict:
    from godot_qa_toolkit.mutation.coverlink import current_tests_face

    rels = [str(Path(t).relative_to(root)) for t in targets]
    return {
        "schema_version": SCHEMA,
        "target_files": rels,
        "file_sha256": {rel: _sha(root, rel) for rel in rels},
        "recipe_hash_pristine": ts._fresh_recipe_hash(str(root)),
        "tests_face": current_tests_face(str(root)),
        "arms": {"gut_full": {"test_to_files": {
            "res://tests/unit/test_game.gd::test_ok": rels,
        }}},
        "arms_state": {"gut_full": {
            "bundles": 1, "pairs": 2, "probes_fired": 3, "state": "valid"}},
        "instrumentable_lines": {rel: [2] for rel in rels},
        # §SPEC-032（C3-R1）：契约 MapView 构造期断言要求 verdict 表在场
        "test_verdicts": {
            "res://tests/unit/test_game.gd::test_ok": {"verdict": "green"}},
    }


def _targets_of(project: Path) -> list[str]:
    return [str(project / "autoload" / "economy_manager.gd"),
            str(project / "systems" / "economy" / "lcps_calculator.gd")]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return _mk_virgin(tmp_path / "proj")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GQT_AUTO_IMPORT", raising=False)
    monkeypatch.delenv("GQT_ATTRIBUTION_MAP", raising=False)
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    ts.reset_ledger()
    ts._fresh_recipe_hash.cache_clear()
    ts._test_scripts_by_name.cache_clear()
    yield
    ts.reset_ledger()


def _assert_no_half_product(project: Path) -> None:
    """半成品门：失败后默认 map 位置不存在或不可加载。"""
    map_path = Path(project) / MAP_REL
    if map_path.is_file():
        with pytest.raises(Exception):
            ts.load_map(str(map_path))


@pytest.fixture()
def cli_seams(monkeypatch, tmp_path):
    """CLI 侧接缝：godot 二进制预检放行（engine seam 已替换，非本判据）。"""
    from godot_qa_toolkit import cli as cli_mod
    from godot_qa_toolkit.mutation import inject as inject_mod

    monkeypatch.setattr(cli_mod, "_apply_godot_bin_or_contract",
                        lambda *a, **k: None)
    monkeypatch.setattr(inject_mod, "godot_command",
                        lambda: str(tmp_path / "godot-stub-bin"))


def _run_risk(project: Path, tmp_path: Path, capsys):
    from godot_qa_toolkit.cli import build_parser, cmd_measure_risk

    report_path = tmp_path / "shadow-report.json"
    args = build_parser().parse_args([
        "measure", "risk",
        "autoload/economy_manager.gd", "systems/economy/lcps_calculator.gd",
        "--project-root", str(project),
        "--report", str(report_path),
    ])
    rc = cmd_measure_risk(args)
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    return rc, out, report_path, captured.err


def _assert_stderr_named(err: str) -> None:
    """§SPEC-009：stderr 非空且含与 stdout 同源的具名原因。"""
    assert err.strip(), "空 stderr 仅留机器信封（§SPEC-009 禁止）"
    assert "attribution map auto-generation failed" in err, err


def test_direct_ensure_map_import_failure_named(project, monkeypatch):
    """直接生成路径：导入失败 → MutationSelectionError 具名（含导入上下文）
    + 台账 ok:false；不留下半成品。"""
    from godot_qa_toolkit import projectroot as pr_mod

    def boom(project_root, **_kw):
        raise RuntimeError(
            f"auto import failed for {project_root}: rc=1 boom "
            "(auto import needs godot)")

    monkeypatch.setattr(pr_mod, "ensure_import_cache", boom)
    with pytest.raises(MutationSelectionError) as ei:
        ensure_map(project, _targets_of(project))
    assert "auto import failed" in str(ei.value)
    events = [e for e in ts.ledger()["events"]
              if e.get("action") in ("generate", "rebuild")]
    assert events and events[-1].get("ok") is False
    assert "auto import failed" in (events[-1].get("error") or "")
    _assert_no_half_product(project)


# --- CLI 逐反例（统一信封：ok:false + failures 具名 + 非零退出）-----------


def test_cli_import_failure_fails_loud(project, cli_seams, monkeypatch,
                                       tmp_path, capsys):
    from godot_qa_toolkit import projectroot as pr_mod

    def boom(project_root, **_kw):
        raise RuntimeError(
            f"auto import failed for {project_root}: rc=1 boom "
            "(auto import needs godot)")

    monkeypatch.setattr(pr_mod, "ensure_import_cache", boom)
    rc, out, _report, err = _run_risk(project, tmp_path, capsys)
    assert rc != 0 and out["ok"] is False, f"假成功被禁止: {out}"
    reasons = json.dumps(out["failures"], ensure_ascii=False)
    assert "auto import failed" in reasons, reasons
    _assert_stderr_named(err)
    _assert_no_half_product(project)


def test_cli_incomplete_import_named(project, cli_seams, monkeypatch,
                                     tmp_path, capsys):
    from godot_qa_toolkit import projectroot as pr_mod

    def partial(project_root, **_kw):
        raise RuntimeError(
            f"auto import did not produce a complete import cache for "
            f"{project_root}: uid_cache.bin missing")

    monkeypatch.setattr(pr_mod, "ensure_import_cache", partial)
    rc, out, _report, err = _run_risk(project, tmp_path, capsys)
    assert rc != 0 and out["ok"] is False
    reasons = json.dumps(out["failures"], ensure_ascii=False)
    assert "complete import cache" in reasons, reasons
    _assert_stderr_named(err)
    _assert_no_half_product(project)


def test_cli_measurement_artifact_missing_named(project, cli_seams,
                                                monkeypatch, tmp_path,
                                                capsys):
    """测量失败（应出产物未落盘）：生成报告后 map 缺失 → CLI 具名非零。"""
    import godot_qa_toolkit.measurement.orchestrator as orch_mod
    from godot_qa_toolkit import projectroot as pr_mod

    monkeypatch.setattr(pr_mod, "ensure_import_cache",
                        lambda root, **kw: None)
    seen = {"n": 0}

    def no_output(root, targets, recipe, *, attribution_out, **_kw):
        seen["n"] += 1
        # 测量自称成功但产物不落盘（=「应出产物未落盘」反例）。
        return None

    monkeypatch.setattr(orch_mod, "run_measurement", no_output)
    rc, out, _report, err = _run_risk(project, tmp_path, capsys)
    assert seen["n"] >= 1
    assert rc != 0 and out["ok"] is False
    reasons = json.dumps(out["failures"], ensure_ascii=False)
    assert "attribution_generate_failed" in reasons or \
        "attribution_map_build_failed" in reasons, reasons
    _assert_stderr_named(err)
    _assert_no_half_product(project)


def test_cli_arms_invalid_named(project, cli_seams, monkeypatch, tmp_path,
                                capsys):
    """arms 无效：测量产出无效执行证据 → 门拒绝 → CLI 具名非零。"""
    import godot_qa_toolkit.measurement.orchestrator as orch_mod
    from godot_qa_toolkit import projectroot as pr_mod

    monkeypatch.setattr(pr_mod, "ensure_import_cache",
                        lambda root, **kw: None)

    def invalid_arms(root, targets, recipe, *, attribution_out, **_kw):
        doc = _fresh_doc(Path(root), list(targets))
        doc["arms_state"] = {"gut_full": {
            "bundles": 0, "pairs": 0, "probes_fired": 0,
            "state": "invalid", "invalid_reason": "no execution evidence"}}
        Path(attribution_out).write_text(json.dumps(doc),
                                         encoding="utf-8")
        return None

    monkeypatch.setattr(orch_mod, "run_measurement", invalid_arms)
    rc, out, _report, err = _run_risk(project, tmp_path, capsys)
    assert rc != 0 and out["ok"] is False
    reasons = json.dumps(out["failures"], ensure_ascii=False)
    assert "invalid arms" in reasons or "no execution evidence" in reasons, \
        reasons
    _assert_stderr_named(err)
    _assert_no_half_product(project)


def test_cli_ledger_flip_failure_stderr_named(project, cli_seams, monkeypatch,
                                              tmp_path, capsys):
    """运行后台账 generate/rebuild 失败翻转终局 ok 时，stderr 同步具名
    （§SPEC-009 第二失败出口）。"""
    import godot_qa_toolkit.measurement.orchestrator as orch_mod
    from godot_qa_toolkit import projectroot as pr_mod
    from godot_qa_toolkit.mutation import coverlink as cl_mod

    monkeypatch.setattr(pr_mod, "ensure_import_cache",
                        lambda root, **kw: None)
    monkeypatch.setattr(cl_mod, "project_coverage_from_map",
                        lambda *a, **k: ({2}, None))

    def ok_run(root, targets, recipe, *, attribution_out, **_kw):
        Path(attribution_out).write_text(
            json.dumps(_fresh_doc(Path(root), list(targets))),
            encoding="utf-8")
        return None

    monkeypatch.setattr(orch_mod, "run_measurement", ok_run)
    # 预温成功后运行中期的生成/重建失败事件（模拟车道内 rebuild 失败留痕）。
    ts._record({"action": "rebuild", "ok": False,
                "error": "mid-run rebuild boom"})
    rc, out, report_path, err = _run_risk(project, tmp_path, capsys)
    assert rc != 0 and out["ok"] is False
    reasons = json.dumps(out["failures"], ensure_ascii=False)
    assert "mid-run rebuild boom" in reasons, reasons
    _assert_stderr_named(err)


def test_skip_measure_ignores_stale_generation_failure_ledger(
        project, cli_seams, monkeypatch, tmp_path, capsys):
    """负控：--skip-measure 不消费映射——同进程台账已有生成失败事件时，
    静态 dry run 仍须 ok:true（台账翻转仅在测量模式下生效）。"""
    from godot_qa_toolkit import projectroot as pr_mod

    def must_not_run(project_root, **_kw):
        raise AssertionError("skip-measure 不得触发导入引导")

    monkeypatch.setattr(pr_mod, "ensure_import_cache", must_not_run)
    ts._record({"action": "generate", "ok": False, "error": "earlier boom"})
    report_path = tmp_path / "shadow-report.json"
    from godot_qa_toolkit.cli import build_parser, cmd_measure_risk

    args = build_parser().parse_args([
        "measure", "risk",
        "autoload/economy_manager.gd", "systems/economy/lcps_calculator.gd",
        "--project-root", str(project),
        "--report", str(report_path), "--skip-measure",
    ])
    rc = cmd_measure_risk(args)
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert rc == 0 and out["ok"] is True, out
    assert "auto-generation failed" not in captured.err, captured.err


def test_skip_measure_stays_static_ok(project, cli_seams, monkeypatch,
                                      tmp_path, capsys):
    """负控：--skip-measure 静态 dry run 不触发导入/生成，保持 ok:true。"""
    from godot_qa_toolkit import projectroot as pr_mod

    def must_not_run(project_root, **_kw):
        raise AssertionError("skip-measure 不得触发导入引导")

    monkeypatch.setattr(pr_mod, "ensure_import_cache", must_not_run)
    report_path = tmp_path / "shadow-report.json"
    from godot_qa_toolkit.cli import build_parser, cmd_measure_risk

    args = build_parser().parse_args([
        "measure", "risk",
        "autoload/economy_manager.gd", "systems/economy/lcps_calculator.gd",
        "--project-root", str(project),
        "--report", str(report_path), "--skip-measure",
    ])
    rc = cmd_measure_risk(args)
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert rc == 0 and out["ok"] is True
    # §SPEC-009：非生成失败路径（含 skip-measure 成功臂）不得新增错误输出。
    assert "auto-generation failed" not in captured.err, captured.err
