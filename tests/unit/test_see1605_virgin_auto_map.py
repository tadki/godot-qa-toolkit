"""SEE-1605 §SPEC-001：virgin 项目首触 auto attribution map 的导入引导。

缺陷（SEE-1604 R1 复验 N1，master `c9f5e75` 既存）：virgin 副本缺 `.godot`
导入缓存时，`measure risk` 首触的 auto 生成静默落空——插桩 GUT 零命中、
生成不落盘，全部 coverage 车道 run_error 而 CLI 仍 `ok:true`。

判据（小夹具 + 测试替身）：
- 直接 `ensure_map` 与 `measure risk` 首触均在首个插桩 GUT（run_measurement）
  前完成导入引导（调用序：import 先于测量）；
- 成功产物落盘于约定默认位置且 `load_map` 可加载；
- 跨目录目标（autoload/ 与 systems/ 两文件）不遗漏；
- warmed 路径复用既有产物：零重复导入、零重复测量。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import truth_select as ts
from godot_qa_toolkit.mutation.truth_select import ensure_map, load_map

SCHEMA = "test-attribution/7"

ELIGIBLE_A = "func a():\n\treturn 1\n"
ELIGIBLE_B = "func b():\n\treturn 2\n"


def _mk_virgin(root: Path) -> Path:
    """忠实 virgin 夹具：真实 Godot 项目形态（project.godot + gutconfig +
    跨目录两份 eligible 生产文件 + 一份测试），**无 `.godot` 导入缓存**。"""
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
    # C3-R1 F1：建图面 = production 宇宙（精确目标）——测试须真实触达
    # 目标，探针才有点火证据（此前靠 root 回退把测试文件自身当目标，
    # 探针证据是假的；F1 修正后该夹具必须真实触达）
    (root / "tests" / "unit" / "test_game.gd").write_text(
        'extends GutTest\nconst A = preload("res://autoload/'
        'economy_manager.gd")\n'
        'const B = preload("res://systems/economy/lcps_calculator.gd")\n'
        "func test_ok() -> void:\n"
        "\tassert_eq(A.new().a(), 1)\n"
        "\tassert_eq(B.new().b(), 2)\n",
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
    """可全链通过 `_evaluate` 新鲜性判定的合成产物（身份字段全部现算）。"""
    from godot_qa_toolkit.mutation.coverlink import current_tests_face

    rels = [str(Path(t).relative_to(root)) for t in targets]
    doc = {
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
    return doc


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return _mk_virgin(tmp_path / "proj")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离环境：auto 模式 + 无显式 map + 未触发逃生门；台账清零。"""
    monkeypatch.delenv("GQT_AUTO_IMPORT", raising=False)
    monkeypatch.delenv("GQT_ATTRIBUTION_MAP", raising=False)
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    ts.reset_ledger()
    ts._fresh_recipe_hash.cache_clear()
    ts._test_scripts_by_name.cache_clear()
    yield
    ts.reset_ledger()


class _Recorder:
    """两个引擎接缝的调用序记录器（import 与插桩 GUT 的先后可断言）。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    def fake_ensure_import_cache(self, project_root, **_kw):
        root = Path(project_root)
        # 真实导入的副产物标记：供下游断言「导入确已发生」而非空记录。
        gd = root / ".godot"
        gd.mkdir(exist_ok=True)
        (gd / "uid_cache.bin").write_text("imported", encoding="utf-8")
        self.events.append("import")
        return None

    def fake_run_measurement(self, root, targets, recipe, *,
                             attribution_out, **_kw):
        self.events.append("measure")
        root = Path(root)
        if not (root / ".godot" / "uid_cache.bin").is_file():
            # virgin 物理事实（QA 实测 N1）：缺导入缓存 → 插桩 GUT 无法加载
            # → probes=0、臂 invalid——这正是静默落空的生成形态。
            doc = _fresh_doc(root, list(targets))
            doc["arms_state"] = {"gut_full": {
                "bundles": 1, "pairs": 0, "probes_fired": 0,
                "state": "invalid",
                "invalid_reason": "instrumented gut failed to load"}}
        else:
            doc = _fresh_doc(root, list(targets))
        Path(attribution_out).write_text(
            json.dumps(doc), encoding="utf-8")
        return None


@pytest.fixture()
def rec(monkeypatch) -> _Recorder:
    """把两个引擎接缝替换为记录替身；gqt 自身选面/投影编排保持真实。"""
    import godot_qa_toolkit.measurement.orchestrator as orch_mod
    from godot_qa_toolkit import cli as cli_mod
    from godot_qa_toolkit import projectroot as pr_mod
    from godot_qa_toolkit.mutation import coverlink as cl_mod
    from godot_qa_toolkit.mutation import inject as inject_mod

    r = _Recorder()
    monkeypatch.setattr(pr_mod, "ensure_import_cache",
                        r.fake_ensure_import_cache)
    monkeypatch.setattr(orch_mod, "run_measurement",
                        r.fake_run_measurement)
    monkeypatch.setattr(cl_mod, "project_coverage_from_map",
                        lambda *a, **k: ({2}, None))
    # 绝对路径形态跳过 PATH which 预检（该预检已由 hardener 判据覆盖）。
    monkeypatch.setattr(inject_mod, "godot_command",
                        lambda: "/tmp/gqt-see1605-stub/godot")
    # godot 二进制校验不在本判据范围（engine seam 已全部替换）。
    monkeypatch.setattr(cli_mod, "_apply_godot_bin_or_contract",
                        lambda *a, **k: None)
    return r


def test_direct_ensure_map_import_precedes_measurement(project, rec):
    """直接 ensure_map 首触：导入引导先于首个插桩 GUT，且只引导一次。"""
    targets = [str(project / "autoload" / "economy_manager.gd"),
               str(project / "systems" / "economy" / "lcps_calculator.gd")]
    out = ensure_map(project, targets)
    assert rec.events == ["import", "measure"], (
        f"virgin 首触必须先导入后测量，实际顺序: {rec.events}")


def test_direct_ensure_map_persists_loadable_map(project, rec):
    """成功产物落盘于默认位置且 load_map 可加载（无半成品）。"""
    targets = [str(project / "autoload" / "economy_manager.gd"),
               str(project / "systems" / "economy" / "lcps_calculator.gd")]
    out = ensure_map(project, targets)
    assert out and Path(out).is_file()
    assert Path(out) == Path(ts.default_map_path(str(project)))
    doc = load_map(out)
    assert doc["schema_version"] == SCHEMA
    assert (project / ".godot" / "uid_cache.bin").is_file(), \
        "导入副产物在场——引导真实发生而非空转"


def test_direct_ensure_map_keeps_cross_directory_targets(project, rec):
    """跨目录目标不遗漏：两个首层目录下的生产文件全部进入生成目标面。"""
    targets = [str(project / "autoload" / "economy_manager.gd"),
               str(project / "systems" / "economy" / "lcps_calculator.gd")]
    ensure_map(project, targets)
    # fake_run_measurement 收到的 targets 即生成目标面（记录在产物内）。
    doc = load_map(ts.default_map_path(str(project)))
    assert set(doc["target_files"]) == {
        "autoload/economy_manager.gd", "systems/economy/lcps_calculator.gd"}


def test_direct_ensure_map_rebuild_bootstraps_import(project, rec):
    """重建路径（默认位置旧 map 不可用）同样须导入引导：generate 与 rebuild
    在「首个插桩 GUT 前完成导入」上不得有行为分叉。"""
    stale = Path(ts.default_map_path(str(project)))
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("{}", encoding="utf-8")
    targets = [str(project / "autoload" / "economy_manager.gd"),
               str(project / "systems" / "economy" / "lcps_calculator.gd")]
    out = ensure_map(project, targets)
    assert rec.events == ["import", "measure"], (
        f"重建路径必须先导入后测量，实际顺序: {rec.events}")
    assert load_map(out)["schema_version"] == SCHEMA


def test_direct_ensure_map_incomplete_cache_still_bootstraps(project, rec):
    """`.godot` 目录在场但导入产物不全 → 仍须调用导入引导（存在 ≠ 完整）。"""
    (project / ".godot").mkdir()
    targets = [str(project / "autoload" / "economy_manager.gd"),
               str(project / "systems" / "economy" / "lcps_calculator.gd")]
    ensure_map(project, targets)
    assert rec.events == ["import", "measure"], (
        f"不完整缓存不得跳过导入引导，实际顺序: {rec.events}")
    assert (project / ".godot" / "uid_cache.bin").is_file(), \
        "导入引导须真实执行并补全产物"


def test_warmed_reuse_skips_import_and_measurement(project, rec):
    """warmed 路径：产物已在场且新鲜 → 直接复用，零导入零测量。"""
    targets = [str(project / "autoload" / "economy_manager.gd"),
               str(project / "systems" / "economy" / "lcps_calculator.gd")]
    ensure_map(project, targets)
    assert rec.events == ["import", "measure"]
    out2 = ensure_map(project, targets)
    assert out2 and Path(out2).is_file()
    assert rec.events == ["import", "measure"], (
        f"warmed 复用不得重复导入/测量，实际: {rec.events}")


def test_cli_measure_risk_virgin_first_touch_succeeds(project, rec,
                                                      tmp_path, capsys):
    """`measure risk` 首触（CLI 全链）：导入先于 GUT，车道零 run_error，
    CLI ok:true 退出 0，报告可消费。"""
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
    assert rc == 0 and out["ok"] is True and out["tool"] == "measure.risk"
    # §SPEC-009：成功路径不得新增错误输出（stderr 披露仅限生成失败臂）。
    assert "auto-generation failed" not in captured.err, captured.err
    assert rec.events and rec.events.index("import") < \
        rec.events.index("measure"), f"导入须先于测量: {rec.events}"
    doc = json.loads(report_path.read_text(encoding="utf-8"))
    assert doc["records"], "报告须含记录车道"
    assert all(r["score_status"] == "scored" for r in doc["records"]), \
        f"映射缺失导致的 run_error 车道不得出现: {doc['records']}"
    assert load_map(ts.default_map_path(str(project)))["schema_version"] == \
        SCHEMA
