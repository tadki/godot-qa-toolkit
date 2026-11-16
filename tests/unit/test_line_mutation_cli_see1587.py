"""SEE-1587 §SPEC-004 — mutation 行级选面 / CLI 判据。

覆盖：
- `--select line` 为默认主语义（fn/file 显式校准视图、full 唯一全脸审计）；
- `--lines` 单行/连续区间/多段规格解析与位点求交披露
  （requested / hit / skipped_no_site；全部无位点显式失败）；
- `--mutation` 消费位点所在行的测试集合（行级并集单 glob），无映射测试
  位点照常执行并披露 no_mapped_tests；
- 默认整文件：按 `frozenset(line→tests)` 签名分组（同签名共享一个 glob，
  禁止一行一组）；无映射测试位点按 uncovered 口径标记不静默丢弃；
- coverage prefilter 消费同轮投影（/1 → /2 → live 投影链，身份/绑定漂移
  逐层披露不假过滤）；显式 `--tests` 披露未使用行级选面。
"""

from __future__ import annotations

import hashlib
import json
import subprocess as sp
from pathlib import Path
from types import SimpleNamespace

import pytest

from godot_qa_toolkit import cli
from godot_qa_toolkit.mutation import planner, runner, truth_select
from godot_qa_toolkit.mutation.coverlink import (
    resolve_coverage_for_prefilter,
    save_coverage,
)
from godot_qa_toolkit.mutation.truth_select import (
    MutationSelectionError,
    parse_lines_spec,
)

from attribution_fixtures import install_recipe

SRC = ("extends RefCounted\n"
       "\n"
       "func add(a, b):\n"
       "\treturn a + b\n"
       "\n"
       "func sub(a, b):\n"
       "\treturn a - b\n"
       "\n"
       "func mul(a, b):\n"
       "\treturn a * b\n"
       "\n"
       "func div(a, b):\n"
       "\treturn a / b\n")

KILL_TAIL = "* test_x\n\t\ttest_x [Failed]"


# ------------------------------------------------------------ CLI / 参数解析


def test_select_default_is_line_and_choices_preserved():
    from godot_qa_toolkit.cli import build_parser
    args = build_parser().parse_args(
        ["mutation", "f.gd", "--project-root", "p"])
    assert args.select_mode == "line"
    for mode in ("line", "fn", "file", "full"):
        a = build_parser().parse_args(
            ["mutation", "f.gd", "--project-root", "p", "--select", mode])
        assert a.select_mode == mode


def test_parse_lines_spec_single_range_multi():
    assert parse_lines_spec("42") == [42]
    assert parse_lines_spec("10-13") == [10, 11, 12, 13]
    assert parse_lines_spec("10-12,30,35-36") == [10, 11, 12, 30, 35, 36]
    assert parse_lines_spec(" 7 , 9-10 ") == [7, 9, 10]


def test_parse_lines_spec_rejects_malformed():
    # "0-5": 区间下界非正（lo<=0 守卫）——单值 "0" 走另一分支；此处钉死
    # 区间形态的非正下界同样拒绝（硬化补盲：TS5 注入存活点）。
    for bad in ("", "0", "0-5", "-3", "20-10", "a", "1-", "1,,2", "3-2"):
        with pytest.raises(ValueError):
            parse_lines_spec(bad)


def test_dispatch_parses_lines_before_any_run(monkeypatch):
    """非法规格在任何 GUT/映射工作前以参数错误契约收口（ok=false）。"""
    args = SimpleNamespace(
        files=["f.gd"], project_root="p", budget=10, timeout=60,
        dry_run=False, tests=None, jobs=None, resume=False,
        mutation_warning=50, timeout_factor=2.0, mutate_all=False,
        mutation=None, lines="20-10", worker_channel=False,
        select_mode="line", attribution_map=None, attribution_auto=True,
        no_reuse_coverage=False, godot_bin=None)
    from godot_qa_toolkit.mutation.truth_select import MutationSelectionError
    # cmd_mutation 捕获 ValueError → ok=false JSON；此处直接验证派发前抛错
    with pytest.raises(ValueError):
        cli._dispatch_mutation(args, "linux")


# ------------------------------------------------------------ planner 卡尺求交


def _fake_mutants(*lines):
    return [SimpleNamespace(stable_id=f"f.gd#s#i{i}", line=ln)
            for i, ln in enumerate(lines)]


def test_plan_selection_discloses_requested_hit_skipped():
    mutants = _fake_mutants(4, 4, 7, 10)
    store = SimpleNamespace(load_entries=lambda: ("prior",))
    disclosures: dict = {}
    prior, _, _, disc, executed, _ = planner._plan_selection(
        store, mutants, None, [4, 6, 7], disclosures)
    assert prior == ("prior",)
    assert [m.line for m in executed] == [4, 4, 7]
    assert disc["lines_selection"] == {
        "requested": [4, 6, 7], "requested_count": 3,
        "hit": [4, 7], "hit_count": 2, "skipped_no_site": [6],
    }
    assert disc["selected"] is True and disc["executed"] == 3


def test_plan_selection_all_no_site_fails_loud():
    mutants = _fake_mutants(4, 7)
    store = SimpleNamespace(load_entries=lambda: {})
    with pytest.raises(ValueError, match="no mutation sites"):
        planner._plan_selection(store, mutants, None, [99, 100], {})


def test_plan_selection_unknown_id_still_fails_loud():
    store = SimpleNamespace(load_entries=lambda: {})
    with pytest.raises(ValueError, match="no mutation sites"):
        planner._plan_selection(store, _fake_mutants(4), ["nope#x#1"], None, {})


# ------------------------------------------------------------ 行级选面解析


def _project(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub\n")
    (proj / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\nfunc t1() -> void:\n\tpass\n")
    (proj / "tests" / "unit" / "test_b.gd").write_text(
        "extends GutTest\nfunc t2() -> void:\n\tpass\n")
    target = proj / "calc.gd"
    target.write_text(SRC)
    install_recipe(proj)
    return proj, target


def _map_doc(proj: Path, target: Path,
             line_to_tests: dict[int, list[str]],
             extra_script_lines: "dict[str, dict[int, list[int]]] | None" = None
             ) -> dict:
    """构造与磁盘绑定的 /6 映射（行事实主真值；含 tests_face/file_sha256）。"""
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash
    recipe, error = build_recipe(proj)
    assert error is None, error
    raw = recipe_hash({k: v for k, v in recipe.items()
                       if k != "recipe_hash"})
    rel = str(target.resolve().relative_to(proj.resolve()))
    test_to_lines: dict[str, dict[str, list[int]]] = {}
    for ln, idents in line_to_tests.items():
        for ident in idents:
            test_to_lines.setdefault(ident, {}).setdefault(rel, []).append(ln)
    for ident, by_rel in (extra_script_lines or {}).items():
        for r, lns in by_rel.items():
            test_to_lines.setdefault(ident, {}).setdefault(r, []).extend(lns)
    test_files: dict[str, list[str]] = {}
    for ident in test_to_lines:
        test_files.setdefault(ident, []).append(rel)
    face = {}
    for gd in sorted((proj / "tests").rglob("*.gd")):
        face[str(gd.relative_to(proj))] = hashlib.sha256(
            gd.read_bytes()).hexdigest()
    arm = {
        "test_to_files": {k: sorted(v) for k, v in test_files.items()},
        "test_to_functions": {}, "script_to_files": {},
        "script_to_functions": {},
        "file_to_tests": {rel: sorted(test_files)},
        "file_to_scripts": {},
        "test_to_lines": {k: {r: sorted(v) for r, v in by_rel.items()}
                          for k, by_rel in test_to_lines.items()},
        "script_to_lines": {},
        "line_to_tests": {rel: {str(ln): sorted(ids)
                                for ln, ids in line_to_tests.items()}},
    }
    inst = sorted({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13})
    return {
        "schema_version": "test-attribution/6",
        "report_recipe_hash": raw, "recipe_hash_pristine": raw,
        "project_root": str(proj), "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {rel: hashlib.sha256(
            target.read_bytes()).hexdigest()},
        "target_files": [rel], "tests_face": face,
        "instrumentable_lines": {rel: inst},
        "arms": {"gut_full": arm},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 1}},
        "notes": [],
    }


def _write_map(proj: Path, doc: dict) -> str:
    out = proj / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(out)


def _line_map(proj: Path, target: Path) -> str:
    """行 4/7 → test_a；行 10 → test_b；行 13 无映射。"""
    doc = _map_doc(proj, target, {
        4: ["test_a.gd::t1"],
        7: ["test_a.gd::t1"],
        10: ["test_b.gd::t2"],
    })
    return _write_map(proj, doc)


def test_line_script_sets_handles_res_identity_and_script_lane():
    doc = {
        "arms": {"gut_full": {
            "line_to_tests": {"calc.gd": {
                "4": ["test_a.gd::t1"],
                "7": ["test_a.gd::t1", "test_b.gd::t2"]}},
            "script_to_lines": {"res://tests/unit/test_b.gd": {
                "calc.gd": [13]}},
        }}}
    sets = truth_select._line_script_sets(doc, "calc.gd", {4, 7, 13, 99})
    assert sets[4] == {"test_a.gd"}
    assert sets[7] == {"test_a.gd", "test_b.gd"}
    # 脚本车道行事实（before_all 等）计入该脚本测试面
    assert sets[13] == {"test_b.gd"}
    assert 99 not in sets


def test_resolve_line_group_selection_union_and_no_mapped(tmp_path):
    proj, target = _project(tmp_path)
    _line_map(proj, target)
    sel = truth_select.resolve_line_group_selection(proj, str(target), [4, 7])
    assert sel["source"] == "line_mapping"
    assert sel["no_mapped_tests"] == []
    assert sorted(Path(s).name for s in sel["tests"]) == ["test_a.gd"]
    sel_dir = proj / truth_select.SELECT_ROOT / sel["tests_glob"].split("/")[-2]
    wrappers = sorted(p.name for p in sel_dir.glob("*.gd"))
    assert wrappers == ["test_a.gd"]
    # 混合：7 命中、13 无映射 → 并集只含 test_a，13 进 no_mapped 披露
    sel = truth_select.resolve_line_group_selection(proj, str(target),
                                                    [7, 13, 99])
    assert sel["source"] == "line_mapping"
    assert sel["no_mapped_tests"] == [13, 99]
    # 全部无映射 → no_mapped_tests 终态（无 glob；消费侧决定 uncovered/执行）
    sel = truth_select.resolve_line_group_selection(proj, str(target), [13])
    assert sel["source"] == "no_mapped_tests" and sel["tests"] == []
    assert sel["tests_glob"] is None and sel["no_mapped_tests"] == [13]


def test_resolve_line_group_selection_fails_loud_without_map(tmp_path):
    proj, target = _project(tmp_path)
    # 无映射 → 文件级 fail-loud（不静默回退全脸）
    with pytest.raises(MutationSelectionError):
        truth_select.resolve_line_group_selection(proj, str(target), [4])


# ------------------------------------------------------------ 行级签名分组


def _mutants_for(target: Path):
    from godot_qa_toolkit.mutation.ops import _collect_mutations
    from godot_qa_toolkit.mutation.scopemanifest import build_manifest
    src = target.read_text(encoding="utf-8")
    _, _, mutants, _ = build_manifest(
        src, str(target), _collect_mutations(src, str(target)))
    return mutants


def test_line_group_chunks_groups_by_signature_not_by_line(tmp_path):
    proj, target = _project(tmp_path)
    map_path = _line_map(proj, target)
    mutants = _mutants_for(target)
    by_line = {}
    for m in mutants:
        by_line.setdefault(m.line, []).append(m)
    # 夹具自检：4/7（test_a 签名）与 10（test_b 签名）与 13（无映射）都有位点
    assert {4, 7, 10, 13} <= set(by_line)
    from godot_qa_toolkit.mutation.executor import _line_group_chunks
    selection_meta = {"source": "runtime_mapping",
                      "tests": ["res://tests/unit/test_a.gd",
                                "res://tests/unit/test_b.gd"],
                      "mapping": {"path": map_path}}
    chunks, groups, no_mapped = _line_group_chunks(
        str(target), str(proj), "res://tests/", "linux", mutants,
        selection_meta, None, False, 2.0, 60, 50, False, None,
        run_id="r1", identity=None, worker_channel=None, total_sites=9)
    # 同签名行（4 与 7）共享一个 chunk——组数按测试集合数，不是行数
    assert len(groups) == 2
    sigs = [set(g["signature"]) for g in groups]
    assert {"test_a.gd"} in sigs and {"test_b.gd"} in sigs
    assert len(chunks) == 2
    # 各 chunk 的 glob 只承载该签名的脚本
    for ch, g in zip(chunks, groups):
        sel_dir = proj / truth_select.SELECT_ROOT / \
            ch["tests_glob"].split("/")[-2]
        assert sorted(p.name for p in sel_dir.glob("*.gd")) == g["signature"]
        assert ch["retry_ids"]
    # 无映射行（13）位点：以 Mutant 形态返回（与 prefilter uncovered 同
    # 形态，报告层统一转 uncovered 记录）——不执行、不静默丢弃
    assert no_mapped and {m.line for m in no_mapped} == {13}
    executed_ids = {i for ch in chunks for i in ch["retry_ids"]}
    assert not ({m.stable_id for m in by_line[13]} & executed_ids)


def test_select_line_goes_through_orchestration_not_serial_fast_path(
        tmp_path, monkeypatch):
    """默认 line 分组需要 per-group glob → 单文件也必须走编排路径。"""
    proj, target = _project(tmp_path)
    _line_map(proj, target)
    args = SimpleNamespace(
        files=[str(target)], project_root=str(proj), budget=10, timeout=60,
        dry_run=False, tests=None, jobs=None, resume=False,
        mutation_warning=50, timeout_factor=2.0, mutate_all=False,
        mutation=None, lines=None, worker_channel=False,
        select_mode="line", attribution_map=None, attribution_auto=True,
        no_reuse_coverage=False, godot_bin=None)
    calls = {}
    from godot_qa_toolkit.mutation import executor as ex
    def _fake_run_files(*a, **k):
        calls["mode"] = "files"
        return {"results": [{"summary": {}}], "jobs": 1, "ok": True}

    monkeypatch.setattr(ex, "run_mutation_files", _fake_run_files)
    monkeypatch.setattr(cli, "_run_mutation_serial",
                        lambda *a, **k: calls.setdefault("mode", "serial"))
    cli._dispatch_mutation(args, "linux")
    assert calls.get("mode") == "files"


# ------------------------------------------------------------ 卡尺行级测试面


class _GutRecorder:
    def __init__(self):
        self.globs: list = []

    def __call__(self, root, timeout_s=60, tests_glob=None):
        self.globs.append(tests_glob)
        if len(self.globs) == 1:
            return (0, "pass [passed]")
        return (1, KILL_TAIL)


def _id_on_line(target: Path, line: int) -> str:
    for m in _mutants_for(target):
        if m.line == line:
            return m.stable_id
    raise AssertionError(f"no mutant on line {line}")


def test_mutation_caliper_uses_line_test_set(tmp_path, monkeypatch):
    proj, target = _project(tmp_path)
    _line_map(proj, target)
    rec = _GutRecorder()
    monkeypatch.setattr(runner, "_run_gut_on_project", rec)
    r = runner.run_mutation(
        str(target), str(proj), budget=10, select_ids=[_id_on_line(target, 4)],
        reuse_coverage=False, select_mode="line")
    # 行 4 的测试集 = test_a → 单 glob 只含 test_a（非文件级双脚本面）
    assert rec.globs and all(g and truth_select.SELECT_ROOT in g
                             for g in rec.globs)
    sel_dir = proj / truth_select.SELECT_ROOT / rec.globs[0].split("/")[-2]
    assert sorted(p.name for p in sel_dir.glob("*.gd")) == ["test_a.gd"]
    assert r["summary"]["test_selection"]["source"] == "line_mapping"


def test_mutation_caliper_no_mapped_tests_executes_with_disclosure(
        tmp_path, monkeypatch):
    proj, target = _project(tmp_path)
    _line_map(proj, target)
    rec = _GutRecorder()
    monkeypatch.setattr(runner, "_run_gut_on_project", rec)
    r = runner.run_mutation(
        str(target), str(proj), budget=10,
        select_ids=[_id_on_line(target, 13)], reuse_coverage=False,
        select_mode="line")
    # 无映射测试位点：公平重跑通道照常执行（不静默丢弃），显式披露
    assert r["summary"]["test_selection"]["source"] == "no_mapped_tests"
    assert r["summary"]["test_selection"]["no_mapped_tests"] == [13]
    assert rec.globs  # baseline + mutant 真的跑了


def test_lines_caliper_multi_segment_union_and_disclosure(tmp_path,
                                                          monkeypatch):
    proj, target = _project(tmp_path)
    _line_map(proj, target)
    rec = _GutRecorder()
    monkeypatch.setattr(runner, "_run_gut_on_project", rec)
    # 4-7 覆盖 4（test_a）与 7（test_a）；6 无位点 → skipped 披露
    r = runner.run_mutation(
        str(target), str(proj), budget=10, select_lines=[4, 5, 6, 7],
        reuse_coverage=False, select_mode="line")
    disc = r["summary"].get("lines_selection") or {}
    assert disc.get("requested") == [4, 5, 6, 7]
    assert disc.get("hit") == [4, 7]
    assert disc.get("skipped_no_site") == [5, 6]
    assert r["summary"]["test_selection"]["source"] == "line_mapping"
    sel_dir = proj / truth_select.SELECT_ROOT / rec.globs[0].split("/")[-2]
    assert sorted(p.name for p in sel_dir.glob("*.gd")) == ["test_a.gd"]


def test_lines_caliper_all_no_site_fails_loud(tmp_path, monkeypatch):
    proj, target = _project(tmp_path)
    _line_map(proj, target)
    rec = _GutRecorder()
    monkeypatch.setattr(runner, "_run_gut_on_project", rec)
    with pytest.raises(ValueError, match="no mutation sites"):
        runner.run_mutation(str(target), str(proj), budget=10,
                            select_lines=[999], reuse_coverage=False,
                            select_mode="line")
    assert not rec.globs  # 零 GUT 调用


def test_explicit_tests_discloses_line_selection_not_used(tmp_path,
                                                          monkeypatch):
    proj, target = _project(tmp_path)
    _line_map(proj, target)
    rec = _GutRecorder()
    monkeypatch.setattr(runner, "_run_gut_on_project", rec)
    r = runner.run_mutation(str(target), str(proj), budget=10,
                            tests_glob="res://tests/", reuse_coverage=False,
                            select_mode="line")
    meta = r["summary"]["test_selection"]
    assert meta["source"] == "explicit_tests"
    assert "line-level selection" in meta["reason"]
    assert all(g == "res://tests/" for g in rec.globs)


# ------------------------------------------------------------ prefilter 投影链


def test_prefilter_live_projection_then_cache(tmp_path):
    proj, target = _project(tmp_path)
    map_path = _line_map(proj, target)
    glob = "res://.gqt-attr-select/proj-test/"
    cov, inst, reason, source = resolve_coverage_for_prefilter(
        str(proj), str(target), glob,
        scripts=["res://tests/unit/test_a.gd"])
    assert source == "projection_live" and reason is None
    assert cov == {4, 7}
    assert inst == set(range(1, 14))
    # /2 落盘 → 第二次命中投影缓存
    cov2, inst2, reason2, source2 = resolve_coverage_for_prefilter(
        str(proj), str(target), glob,
        scripts=["res://tests/unit/test_a.gd"])
    assert source2 == "projection_store" and cov2 == {4, 7}
    # 双脚本并集
    cov3, _inst3, _r3, source3 = resolve_coverage_for_prefilter(
        str(proj), str(target), "res://.gqt-attr-select/both/",
        scripts=["res://tests/unit/test_a.gd",
                 "res://tests/unit/test_b.gd"])
    assert source3 == "projection_live" and cov3 == {4, 7, 10}


def test_prefilter_store_takes_priority(tmp_path):
    proj, target = _project(tmp_path)
    _line_map(proj, target)
    glob = "res://tests/"
    save_coverage({2}, 6, str(proj), str(target), glob,
                  instrumentable_lines={1, 2, 3, 4, 5, 6})
    cov, inst, reason, source = resolve_coverage_for_prefilter(
        str(proj), str(target), glob,
        scripts=["res://tests/unit/test_a.gd"])
    assert source == "coverage_store" and cov == {2}


def test_prefilter_stale_map_falls_back_unfiltered_with_reasons(tmp_path):
    proj, target = _project(tmp_path)
    _line_map(proj, target)
    target.write_text(SRC.replace("\treturn a + b", "\treturn a + b + 0"))
    cov, inst, reason, state = resolve_coverage_for_prefilter(
        str(proj), str(target), "res://.gqt-attr-select/x/",
        scripts=["res://tests/unit/test_a.gd"])
    assert cov is None and inst is None
    assert state == "store_miss"
    assert "file binding stale" in (reason or "")


def test_prefilter_identity_drift_reason_kept_in_fallback(tmp_path):
    proj, target = _project(tmp_path)
    glob = "res://tests/"
    from godot_qa_toolkit.mutation.identity import identity_pack
    ident = identity_pack(str(proj), "linux")
    save_coverage({2}, 6, str(proj), str(target), glob,
                  instrumentable_lines={1, 2, 3},
                  identity=dict(ident, gut_addon_hash="drifted"))
    cov, inst, reason, state = resolve_coverage_for_prefilter(
        str(proj), str(target), glob, scripts=None, identity=ident)
    assert cov is None
    assert "identity drift" in (reason or "")


def test_mutation_default_line_mode_uses_projection_prefilter(tmp_path,
                                                              monkeypatch):
    """默认 line 模式单文件串行路径：prefilter 经行事实投影生效，
    不再常态 store miss（披露 source=projection_live）。"""
    proj, target = _project(tmp_path)
    _line_map(proj, target)
    rec = _GutRecorder()
    monkeypatch.setattr(runner, "_run_gut_on_project", rec)
    # 行 10（test_b）无 test_a 覆盖 → 但文件级测试面含 test_a+test_b →
    # 文件级投影覆盖 4/7/10 全部 → 无 uncovered；行 13 无映射 → 不在
    # 文件级测试面的行事实 → 文件级投影未覆盖 → uncovered 标记
    r = runner.run_mutation(str(target), str(proj), budget=50,
                            reuse_coverage=True, select_mode="line")
    pf = r["summary"]["coverage_prefilter"]
    assert pf["source"] == "projection_live"
    uncovered = [f for f in r["failures"] if f["verdict"] == "uncovered"]
    assert all(f["line"] == 13 for f in uncovered)
    assert r["summary"].get("test_selection", {}).get("source") \
        == "runtime_mapping"

def test_projection_matches_res_identity_to_basename_selection():
    """行事实身份是全路径（res://）时脚本集合按 basename 匹配——否则并集
    静默为空 = 全量假 uncovered（真实运行时身份形态回归守卫）。"""
    doc = {
        "schema_version": "test-attribution/6",
        "recipe_hash_pristine": "pristine-1",
        "file_sha256": {}, "tests_face": {}, "instrumentable_lines": {},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 1}},
        "arms": {"gut_full": {
            "test_to_lines": {
                "res://tests/unit/test_a.gd::t1": {
                    "calc.gd": [4, 7]},
            },
            "line_to_tests": {"calc.gd": {
                "4": ["res://tests/unit/test_a.gd::t1"],
                "7": ["res://tests/unit/test_a.gd::t1"]}},
            "script_to_lines": {
                "res://tests/unit/test_b.gd": {"calc.gd": [10]}},
        }},
    }
    # 用内部归一逻辑验证：跨层身份（全路径 vs 裸名）都能命中
    from pathlib import Path as _P
    names_ident = {"x"}
    assert _P("res://tests/unit/test_a.gd".split("::", 1)[0]).name \
        == _P("test_a.gd").name
    # 端到端：_line_script_sets 同时接受两种形态（上方单测），投影侧
    # basename 归一由同一 Path(...).name 口径保证——直接断言脚本集合包含
    # 全路径身份的 basename
    sets = truth_select._line_script_sets(doc, "calc.gd", {4, 10})
    assert sets[4] == {"test_a.gd"}
    assert sets[10] == {"test_b.gd"}


def test_all_no_mapped_sites_settle_uncovered_result(tmp_path, monkeypatch):
    """默认 line 模式 + 取消 prefilter：全部位点落在无映射行 → 定终态
    uncovered（零执行、报告留痕、不进 ok 门禁）。"""
    proj, target = _project(tmp_path)
    # 映射只覆盖无变异位点的行 2（契约仍成立：文件级选面有效）
    doc = _map_doc(proj, target, {2: ["test_a.gd::t1"]})
    map_path = _write_map(proj, doc)
    from godot_qa_toolkit.mutation import executor as ex
    chunks, ctx, settled = ex._plan_file_chunks(
        str(target), str(proj), 50, 60, "res://tests/", 1, False, "linux",
        50, 2.0, False, False,
        {"res://tests/": {"rc": 0, "tail": "", "elapsed": 0.1}},
        auto_selection={
            "source": "runtime_mapping",
            "tests": ["res://tests/unit/test_a.gd"],
            "mapping": {"path": map_path}}, select_mode="line")
    assert chunks == [] and ctx is None and settled is not None
    assert settled["ok"] is True
    assert settled["summary"]["uncovered"] == len(settled["failures"]) > 0
    assert all(f["verdict"] == "uncovered" for f in settled["failures"])
    assert settled["summary"]["file"] == str(target)


def test_prefilter_projection_handles_res_identity(tmp_path):
    """端到端回归：映射行事实身份为全路径（res://）时投影并集仍非空——
    修复前按裸名比对会静默产出空 covered（全量假 uncovered）。"""
    proj, target = _project(tmp_path)
    doc = _map_doc(proj, target, {
        4: ["res://tests/unit/test_a.gd::t1"],
        7: ["res://tests/unit/test_a.gd::t1"],
    })
    _write_map(proj, doc)
    cov, inst, reason, source = resolve_coverage_for_prefilter(
        str(proj), str(target), "res://.gqt-attr-select/res-ident/",
        scripts=["res://tests/unit/test_a.gd"])
    assert source == "projection_live" and reason is None
    assert cov == {4, 7}


def test_merge_results_passes_coverage_prefilter_through():
    """编排合并必须透出 prefilter 来源（§SPEC-006 证据面）：dict 计数跨分片
    求和、source 取首个非空；字符串 miss 态保持最后写入者。"""
    from godot_qa_toolkit.mutation.executor import _merge_results
    chunks = [{"file": "a.gd", "total_sites": 4, "attention": None},
              {"file": "a.gd", "total_sites": 4, "attention": None}]
    results = [
        {"ok": True, "summary": {"coverage_prefilter": {
            "source": "projection_live", "uncovered": 1,
            "covered_executed": 2}, "mutants": 2},
         "failures": []},
        {"ok": True, "summary": {"coverage_prefilter": {
            "source": "projection_store", "uncovered": 2,
            "covered_executed": 1}, "mutants": 1},
         "failures": []},
    ]
    merged = _merge_results(chunks, results, jobs=2)
    pf = merged["results"][0]["summary"]["coverage_prefilter"]
    assert pf == {"source": "projection_live", "uncovered": 3,
                  "covered_executed": 3}


def test_line_group_chunk_carries_group_test_set(tmp_path):
    """行级分组 chunk 携带组内测试集（worker prefilter 投影按组面，不用
    文件级并集冒充）。"""
    proj, target = _project(tmp_path)
    map_path = _line_map(proj, target)
    mutants = _mutants_for(target)
    from godot_qa_toolkit.mutation.executor import _line_group_chunks
    selection_meta = {"source": "runtime_mapping",
                      "tests": ["res://tests/unit/test_a.gd",
                                "res://tests/unit/test_b.gd"],
                      "mapping": {"path": map_path}}
    chunks, groups, _nm = _line_group_chunks(
        str(target), str(proj), "res://tests/", "linux", mutants,
        selection_meta, None, False, 2.0, 60, 50, False, None,
        run_id="r1", identity=None, worker_channel=None, total_sites=9)
    assert chunks
    for ch, g in zip(chunks, groups):
        # 组面脚本 ⊆ 文件级并集，且与签名一致
        assert sorted(Path(t).name for t in
                      ch["selection_meta"]["tests"]) == g["signature"]
        assert ch["selection_meta"]["source"] == "runtime_mapping"
        assert ch["selection_mode"] == "line"
