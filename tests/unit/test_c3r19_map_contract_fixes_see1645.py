"""SEE-1645 C3-R19：Leader 复核 C3-R18 半成品（`4d8cb1b`）退回的 P1–P5
产品侧缺陷常驻回归（P6–P8 由产品文案/夹具随本轮修复与结构脚本覆盖）。

- P1：地图已新鲜时 `refresh()` 走锁内复检即返回，不再因函数内后置
  import 让 `map_path` 成为未绑定局部名（UnboundLocalError 裸 traceback）。
- P2：`measure map-update` 不带任何路径参数即可完成一次续新，输出
  `mode`（create/increment/full/fresh）与 `already_fresh`；处理函数从
  `.mutation.*` 正确导入，续新失败出具名 JSON。
- P3：覆盖前置过滤的"已解析路径"装载走契约视图——文件路径不再被当
  项目根裸装载（任何路径都不抛 BaselineSourceError）。
- P4：缺 `feature_sha256` 键（其余因素全部新鲜）→ `would_refresh=full`；
  因素文案不被外层再包一层 `feature_sha256: ` 前缀（前缀判断可达）。
- P5：契约自洽——缺判定表时 `refresh` 自行全量重建（不再抛错不动旧
  图）；已删 feature 的判定残留 → `status` 判不新鲜并预言 full。
- P7：续新管线的 alien 异常（gherkin 增量腿的 GherkinRunError 一族）
  以契约错误类型（MapContractError）出 `refresh`，旧图字节不变。
"""

from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_project(tmp_path: Path) -> Path:
    """最小合法 gqt 项目形状（.gutconfig + addon 元数据 + demo.feature
    + 一生产文件 + 一测试文件）。返回项目根。"""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata

    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    install_recipe(proj)
    ensure_addon_metadata(proj)
    (proj / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (proj / "tests" / "unit" / "test_a.gd").write_text("extends GutTest\n",
                                                       encoding="utf-8")
    (proj / "prod.gd").write_text("extends Node\n", encoding="utf-8")
    return proj


def _fresh_doc(proj: Path) -> dict:
    """契约认可的"新鲜"地图文档：schema/臂/pristine/report_hash/内容面
    /tests_face/判定表/feature 指纹全部与树一致。"""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import _feature_sha_of

    from godot_qa_toolkit.measurement.increment import _inject_toolchain
    from godot_qa_toolkit.measurement.recipe import build_recipe
    from godot_qa_toolkit.mutation.truth_select import _fresh_recipe_hash

    recipe, error = build_recipe(proj)
    assert error is None, error
    return {
        "schema_version": "test-attribution/7",
        "report_recipe_hash": _inject_toolchain(proj,
                                                dict(recipe))["recipe_hash"],
        "recipe_hash_pristine": _fresh_recipe_hash(str(proj)),
        "project_root": str(proj),
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {"prod.gd": _sha256(proj / "prod.gd")},
        "target_files": ["prod.gd"],
        "tests_face": {"tests/unit/test_a.gd":
                       _sha256(proj / "tests" / "unit" / "test_a.gd")},
        "instrumentable_lines": {"prod.gd": [1, 2]},
        "arms": {"gut_full": {
            "test_to_lines": {}, "script_to_lines": {}, "tests": 0,
            "scripts": 0, "files": 0, "pairs": 0, "function_pairs": 0,
            "test_to_functions": {}, "script_to_functions": {},
            # 基线边（file 边）：无边的生产文件在增量分类里判 prod_new
            # （不猜失效面 → 具名全量回退），有边才走 prod_changed 增量。
            "file_to_tests": {"prod.gd":
                              ["tests/unit/test_a.gd::test_a"]},
            "file_to_scripts": {}, "line_pairs": 0,
            "script_line_pairs": 0, "line_to_tests": {}}},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 0, "probes_fired": 5,
                                    "vacuous": False}},
        "test_verdicts": {},
        "notes": [],
        "feature_sha256": _feature_sha_of(proj),
    }


def _write_map(proj: Path, doc: dict) -> Path:
    map_file = proj / ".qa-cache" / "attribution-map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    map_file.write_text(json.dumps(doc, ensure_ascii=False),
                        encoding="utf-8")
    return map_file


# 消费方传入的生产目标面（mutation/coverage 同形态：只传生产文件；
# 测试文件经 tests_face 对账，不进 files——传了会被增量面当新增文件）
_FILES = ["prod.gd"]


def _run_cli(argv: list) -> tuple:
    from godot_qa_toolkit import cli as cli_mod

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ─────────────────────────────── P1 ───────────────────────────────
def test_refresh_fresh_map_returns_view_without_unbound_error(tmp_path):
    """地图已新鲜：refresh() 锁内复检即返回视图（4d8cb1b 上因函数内后置
    import 抛 UnboundLocalError：mutation / measure risk / measure
    map-update 裸 traceback，coverage 报 attribution_map_build_failed）。"""
    proj = _make_project(tmp_path)
    map_file = _write_map(proj, _fresh_doc(proj))
    from godot_qa_toolkit.mutation.map_contract import MapView, refresh

    outcome = refresh(str(proj), list(_FILES))
    assert outcome.get("fresh") is True
    assert outcome.get("already_fresh") is True
    view = outcome.get("view")
    assert isinstance(view, MapView)
    assert view.map_path == str(map_file)


# ─────────────────────────────── P2 ───────────────────────────────
def test_map_update_without_path_args_reports_fresh_mode(tmp_path,
                                                         monkeypatch):
    """`measure map-update` 无位置参数、只带 --project-root 即可直接成功，
    输出说明本次是哪一种（mode=fresh + already_fresh）。（4d8cb1b 上
    roots 仍必填 + 处理函数 import 路径错误，入口整体不可用。）"""
    proj = _make_project(tmp_path)
    map_file = _write_map(proj, _fresh_doc(proj))
    _bypass_entry_helpers(monkeypatch, proj)
    rc, out, err = _run_cli(["measure", "map-update",
                             "--project-root", str(proj)])
    assert rc == 0, err
    assert "Traceback" not in err
    payload = json.loads(out)
    assert payload.get("ok") is True
    summary = payload.get("summary") or {}
    assert summary.get("mode") == "fresh", summary
    assert summary.get("already_fresh") is True
    assert summary.get("map_path") == str(map_file)


def test_map_update_no_map_reports_create_and_named_error(tmp_path,
                                                          monkeypatch):
    """无地图：mode 预言 create；续新失败时出口仍是具名 JSON（零
    traceback）。（4d8cb1b 上处理函数 import 即崩，入口整体不可用。）"""
    proj = _make_project(tmp_path)
    _bypass_entry_helpers(monkeypatch, proj)
    import godot_qa_toolkit.mutation.map_contract as _mc
    from godot_qa_toolkit.mutation.map_contract import MapContractError

    def boom(*_a, **_k):
        raise MapContractError(
            "renewal pipeline failed: (probe) gherkin leg red")

    monkeypatch.setattr(_mc, "refresh", boom)
    rc, out, err = _run_cli(["measure", "map-update",
                             "--project-root", str(proj)])
    assert rc == 1
    assert "Traceback" not in err
    payload = json.loads(out)
    assert payload.get("ok") is False
    summary = payload.get("summary") or {}
    assert "renewal pipeline failed" in summary.get("error", "")
    assert summary.get("mode") == "create"


def _bypass_entry_helpers(monkeypatch, proj: Path) -> None:
    """入口 helper 单元替换：项目根解析与 godot-bin 注入与本测无关
    （聚焦 map-update 处理函数契约：导入面 / mode / 具名出口）。"""
    import godot_qa_toolkit.cli as cli_mod

    def fake_root(tool, args, *, target=None):
        args.project_root = str(proj)
        return None

    monkeypatch.setattr(cli_mod, "_resolve_project_root_or_contract",
                        fake_root)
    monkeypatch.setattr(cli_mod, "_apply_godot_bin_or_contract",
                        lambda tool, args: None)


# ─────────────────────────────── P3 ───────────────────────────────
def test_prefilter_resolved_path_loads_doc_via_contract_view(tmp_path):
    """覆盖前置过滤拿到"已解析路径"时：doc 一律取自契约视图，文件路径
    不再被当项目根裸装载（4d8cb1b 上 read_doc_at 对任何路径都抛
    BaselineSourceError → prefilter 链裸崩）。"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc(proj)
    map_file = _write_map(proj, doc)
    from godot_qa_toolkit.mutation.coverlink import _prefilter_resolve_map

    reasons: list = []
    resolved, loaded = _prefilter_resolve_map(str(proj), str(map_file),
                                              reasons)
    assert resolved == str(map_file)
    assert loaded is not None, reasons
    assert loaded.get("schema_version") == doc["schema_version"]
    assert reasons == []


# ─────────────────────────────── P4 ───────────────────────────────
def test_missing_feature_sha256_key_predicts_full_when_otherwise_fresh(
        tmp_path):
    """缺 `feature_sha256` 键（其余因素全部新鲜）→ `would_refresh=full`。
    （4d8cb1b 上因素文本被外层再包一层 `feature_sha256: `，前缀判断
    永不命中 → 落进增量判定。）"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc(proj)
    doc.pop("feature_sha256")
    _write_map(proj, doc)
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(proj), list(_FILES))
    assert st.get("fresh") is False
    assert st.get("would_refresh") == "full", st.get("would_refresh")
    assert any(f.startswith("feature_sha256_missing")
               for f in st.get("factors") or []), st.get("factors")


# ─────────────────────────────── P5 ───────────────────────────────
def test_refresh_rebuilds_when_verdict_table_missing(tmp_path, monkeypatch):
    """契约自洽：缺判定表（其余因素新鲜）→ refresh 自行全量重建补表，
    不再抛错不动旧图、之后每次都失败（4d8cb1b 上预言 increment、漂移
    状态无可续新内容、什么都不做 → 装填后校验抛 MapContractError）。"""
    proj = _make_project(tmp_path)
    stale = _fresh_doc(proj)
    stale.pop("test_verdicts")
    _write_map(proj, stale)
    from godot_qa_toolkit.mutation import truth_select as _ts

    calls: list = []

    def fake_ensure(root, files, timeout_s=600, force=False, **_kw):
        calls.append((root, bool(force)))
        _write_map(proj, _fresh_doc(proj))
        return None

    monkeypatch.setattr(_ts, "ensure_map", fake_ensure)
    from godot_qa_toolkit.mutation.map_contract import refresh

    outcome = refresh(str(proj), list(_FILES))
    assert calls, "缺判定表必须触发一次全量重建（ensure_map force）"
    assert calls[0][1] is True, "重建必须走 force 原子替换路径"
    assert outcome.get("fresh") is True
    after = json.loads((proj / ".qa-cache" / "attribution-map.json")
                       .read_text(encoding="utf-8"))
    assert "test_verdicts" in after


def test_dropped_feature_residue_judged_stale_and_full(tmp_path):
    """契约自洽：已删 feature 的判定残留（其余因素新鲜）→ status 判
    不新鲜并预言 full（4d8cb1b 上视图构造失败被吞、仍判 fresh）。"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc(proj)
    doc["increment"] = {"gherkin_dropped_features": ["zz_probe"]}
    doc["test_verdicts"] = {"gherkin://feature/zz_probe::s::1":
                            {"verdict": "green",
                             "classification": "pre_existing"}}
    _write_map(proj, doc)
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(proj), list(_FILES))
    assert st.get("fresh") is False
    assert st.get("would_refresh") == "full", st.get("would_refresh")
    assert any(f.startswith("view:") for f in st.get("factors") or []), \
        st.get("factors")


# ─────────────────────────────── P7 ───────────────────────────────
def test_refresh_wraps_alien_increment_leg_failure(tmp_path, monkeypatch):
    """续新管线失败（gherkin 增量腿的 alien 异常一族）→ 以契约错误类型
    出 refresh（4d8cb1b 上 GherkinRunError 一族原样上抛 → mutation /
    map-update 裸 traceback），且旧图字节不变。"""
    proj = _make_project(tmp_path)
    map_file = _write_map(proj, _fresh_doc(proj))
    before = map_file.read_bytes()
    (proj / "prod.gd").write_text("extends Node\n# drifted\n",
                                  encoding="utf-8")
    import godot_qa_toolkit.measurement.increment as _inc

    def boom(*_a, **_k):
        raise RuntimeError(
            "gherkin increment leg failed: preflight red (probe)")

    monkeypatch.setattr(_inc, "run_attribution_increment", boom)
    from godot_qa_toolkit.mutation.map_contract import (
        MapContractError, refresh,
    )

    with pytest.raises(MapContractError) as ei:
        refresh(str(proj), list(_FILES))
    assert "gherkin" in str(ei.value)
    assert map_file.read_bytes() == before, "旧图字节不变"


# ────────────────────────── D1（C3-R21 判定处置）──────────────────────
def test_status_names_report_recipe_hash_drift(tmp_path):
    """C3-R21 D1（QA HIGH 判定：环境差异形态）：内容面漂移 + 地图存储的
    report_recipe_hash 与现算注入哈希不同（建图后工具链/环境漂移，例：
    GQT_GODOT_BIN 变更或缺失）→ 增量资格闸恒假、决策必为具名全量——
    status 把该原因列进 factors 并预言 full。此前因素只有内容面
    （feature_sha256: changed），预言 full 无具名原因。"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc(proj)
    doc["report_recipe_hash"] = "stored-build-hash-0000"  # 与现算必然不同
    _write_map(proj, doc)
    # 内容面漂移（QA 场景：feature 内容改动）走决策镜像
    demo = proj / "addons" / "gqt_gherkin" / "features" / "demo.feature"
    demo.write_text(demo.read_text(encoding="utf-8")
                    + "\n# c3r21 d1 probe\n", encoding="utf-8")
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(proj), list(_FILES))
    assert st.get("fresh") is False
    assert st.get("would_refresh") == "full", st.get("would_refresh")
    assert any(f.startswith("report_recipe_hash:")
               for f in st.get("factors") or []), st.get("factors")


# ──────────────────────────── F1（C3-R20）────────────────────────────
def test_refresh_planned_full_with_content_routes_through_named_leg(
        tmp_path, monkeypatch):
    """C3-R20 F1：预言 full 且内容面有可续新改动（周期校准到期 + 内容
    漂移）→ refresh 仍经增量腿，腿的具名全量回退把原因写进地图台账。
    （61b293c 上"预言 full"被一律直接重建：原因既不进输出也不进台账，
    经增量腿的具名回退在生产路径不可达——Leader 复核退回项。）"""
    proj = _make_project(tmp_path)
    doc = _fresh_doc(proj)
    doc["increment"] = {"increments_since_full": 5}
    map_file = _write_map(proj, doc)
    (proj / "prod.gd").write_text("extends Node\n# drifted\n",
                                  encoding="utf-8")
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(proj), list(_FILES))
    assert st.get("would_refresh") == "full", st

    import godot_qa_toolkit.measurement.increment as _inc
    from godot_qa_toolkit.mutation import truth_select as _ts

    leg_calls: list = []
    rebuild_calls: list = []

    def fake_leg(project_root, targets, recipe, baseline, changed, out,
                 **_kw):
        # 模拟增量腿的具名全量回退（_fallback_full_face）：重建图并按
        # 台账契约写入 `increment.reason`
        leg_calls.append(list(changed))
        rebuilt = _fresh_doc(proj)
        rebuilt["increment"] = {"mode": "calibration_due_fallback",
                                "reason": "calibration_due_fallback",
                                "changed_files": {},
                                "increments_since_full": 0}
        Path(out).write_text(json.dumps(rebuilt), encoding="utf-8")
        return {"mode": "calibration_due_fallback"}

    def fake_rebuild(root, files, timeout_s=600, force=False, **_kw):
        rebuild_calls.append(1)
        _write_map(proj, _fresh_doc(proj))
        return None

    monkeypatch.setattr(_inc, "run_attribution_increment", fake_leg)
    monkeypatch.setattr(_ts, "ensure_map", fake_rebuild)
    from godot_qa_toolkit.mutation.map_contract import refresh

    outcome = refresh(str(proj), list(_FILES))
    assert outcome.get("fresh") is True
    assert leg_calls, "预言 full 且内容面可续新时必须经增量腿（具名回退可达）"
    assert not rebuild_calls, "内容面可续新时不得绕过具名回退直接重建"
    after = json.loads(map_file.read_text(encoding="utf-8"))
    assert (after.get("increment") or {}).get("reason") == \
        "calibration_due_fallback", "全量原因必须进地图台账"
