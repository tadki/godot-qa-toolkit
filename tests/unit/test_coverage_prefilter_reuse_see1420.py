"""§SPEC-1420-12：P2 coverage 前置过滤 + `--reuse-coverage`（定案 L3/D7）。

SEE-1594 S2（A04/A05）迁移：/1 手动 store 退役——prefilter 数据源只剩
映射派生链（/2 投影缓存绑定映射内容与执行证据 → live 现场投影）。
covered 位点 verdict 与全量跑一致 + uncovered 全部显式标记（不进 ok 门禁、
不消耗预算）；P3 卡尺（--mutation/--lines）绕过前置过滤——随机抽查被剔
位点防假阴性的通道。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import attribution_fixtures as fx
from godot_qa_toolkit.mutation import coverlink
from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation import truth_select
from godot_qa_toolkit.mutation.identity import identity_pack
from godot_qa_toolkit.mutation.inject import detect_binary_kind

IDENT = "res://tests/test_x.gd::t1"
GLOB = "res://tests/"


def _ident(proj):
    """SEE-1550 L1：prefilter 要求 store 身份包全匹配——测试侧用与
    run_mutation 同源的 pack 落盘（同进程同缓存，godot --version 稳定）。"""
    return identity_pack(str(proj), detect_binary_kind())


SRC = ("func add(a, b):\n"
       "\treturn a + b\n"
       "\n"
       "func sub(a, b):\n"
       "\treturn a - b\n")

KILL_TAIL = "* test_x\n\t\ttest_x [Failed]"


def _write_project(tmp_path, src=SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    # recipe 可先于任何 run_mutation 构建成功——防止 _fresh_recipe_hash 的
    # lru_cache 把早期 None（recipe 不可构建）缓存住毒化后续种子/绑定。
    fx.install_recipe(proj)
    truth_select._fresh_recipe_hash.cache_clear()
    return proj, target


def _seed_projection(proj, target, covered, instrumentable, *, identity=True):
    """经生产写侧落 /2 派生缓存：先写映射（行事实 = covered），再由
    project_coverage_from_map 投影落盘（绑定包与映射内容全绑定）。"""
    rel = "calc.gd"
    map_path = fx.line_facts_map(
        proj, {IDENT: {rel: sorted(covered)}},
        instrumentable={rel: sorted(instrumentable)})
    doc = json.loads(Path(map_path).read_text(encoding="utf-8"))
    scripts = ["res://tests/test_x.gd"]
    got, why = coverlink.project_coverage_from_map(
        str(proj), doc, str(target), scripts, GLOB,
        identity=_ident(proj) if identity else None,
        current_recipe_pristine=truth_select._fresh_recipe_hash(str(proj)))
    assert got is not None, why
    return doc


def _current_doc(proj):
    return truth_select.load_map(str(Path(proj) / ".qa-cache"
                                     / "attribution-map.json"))


class TestProjectionCacheProvenance:
    def test_save_and_load_roundtrip(self, tmp_path):
        proj, target = _write_project(tmp_path)
        _seed_projection(proj, target, {2, 3, 5}, {2, 3, 5})
        lines, reason = coverlink.load_projected_coverage(
            str(proj), str(target), GLOB,
            current_doc=_current_doc(proj),
            current_recipe_pristine=truth_select._fresh_recipe_hash(
                str(proj)))
        assert lines == {2, 3, 5}
        assert reason is None

    def test_source_change_invalidates(self, tmp_path):
        proj, target = _write_project(tmp_path)
        _seed_projection(proj, target, {2, 3}, {2, 3})
        target.write_text(SRC + "\n# touched\n")
        lines, reason = coverlink.load_projected_coverage(
            str(proj), str(target), GLOB)
        assert lines is None
        assert "source changed" in reason

    def test_glob_mismatch_invalidates(self, tmp_path):
        proj, target = _write_project(tmp_path)
        _seed_projection(proj, target, {2}, {2})
        lines, reason = coverlink.load_projected_coverage(
            str(proj), str(target), "res://tests/unit/")
        assert lines is None
        assert "tests_glob" in reason

    def test_missing_cache_degrades_with_reason(self, tmp_path):
        proj, target = _write_project(tmp_path)
        lines, reason = coverlink.load_projected_coverage(
            str(proj), str(target), GLOB)
        assert lines is None
        assert "no projected coverage" in reason


class TestCoveragePrefilter:
    def test_uncovered_sites_marked_and_not_executed(self, tmp_path,
                                                     monkeypatch):
        proj, target = _write_project(tmp_path)
        # 行 2（add 体）覆盖；行 5（sub 体）未覆盖；位点行全部可插桩（D3）
        _seed_projection(proj, target, {2}, {2, 5})
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob=GLOB,
                                 reuse_coverage=True)
        uncovered = [f for f in r["failures"] if f["verdict"] == "uncovered"]
        assert len(uncovered) == 3  # sub 的 3 个位点全部显式标记
        assert all(f["line"] == 5 for f in uncovered)
        # covered 位点照常执行
        assert r["summary"]["coverage_prefilter"] == {
            "source": "projection_store", "uncovered": 3,
            "covered_executed": 3}
        assert r["summary"]["killed"] == 3

    def test_uncovered_not_in_ok_gate(self, tmp_path, monkeypatch):
        """uncovered 不计入 ok 门禁失败档——quality 信号非工具失败。"""
        proj, target = _write_project(tmp_path)
        _seed_projection(proj, target, {2}, {2, 5})

        state = {"n": 0}

        def kill_all(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            if state["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob=GLOB,
                                 reuse_coverage=True)
        assert r["ok"] is True  # covered 全 killed + uncovered 显式跳过

    def test_store_miss_runs_unfiltered_with_disclosure(self, tmp_path,
                                                        monkeypatch,
                                                        capsys):
        proj, target = _write_project(tmp_path)

        state = {"n": 0}

        def kill_all(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            if state["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob=GLOB,
                                 reuse_coverage=True)
        assert r["summary"]["coverage_prefilter"] == "store_miss"
        assert "coverage store miss" in capsys.readouterr().err
        assert r["summary"]["killed"] == 6  # 未过滤

    def test_prefilter_before_budget(self, tmp_path, monkeypatch):
        """预算只花在 covered 位点上——uncovered 不消耗预算（前置语义）。"""
        src = ("func one(a):\n"
               "\treturn a + 1\n"
               "\n"
               "func two(a):\n"
               "\treturn a + 2\n")
        proj, target = _write_project(tmp_path, src)
        # 只覆盖 two 体（行 5）；one（行 2）uncovered；位点行全可插桩（D3）
        _seed_projection(proj, target, {5}, {2, 5})
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob=GLOB,
                                 reuse_coverage=True)
        s = r["summary"]
        # one 的 5 位点 uncovered 显式标记；two 的 3 位点 covered，budget=2 截断
        assert s["coverage_prefilter"] == {
            "source": "projection_store", "uncovered": 5,
            "covered_executed": 2}
        assert s["executed"] == 2
        uncovered = [f for f in r["failures"] if f["verdict"] == "uncovered"]
        assert len(uncovered) == 5


class TestCoveredVerdictConsistency:
    def test_covered_verdicts_match_full_run(self, tmp_path, monkeypatch):
        """§12 验收口径：covered 位点 verdict 集合与全量跑完全一致。"""
        proj, target = _write_project(tmp_path)

        def mk_kill_all():
            state = {"n": 0}

            def kill_all(root, timeout_s=60, tests_glob=None):
                state["n"] += 1
                if state["n"] == 1:
                    return (0, "baseline ok")
                return (1, KILL_TAIL)

            return kill_all

        monkeypatch.setattr(mrunner, "_run_gut_on_project", mk_kill_all())
        full = mrunner.run_mutation(str(target), str(proj), budget=10,
                                    tests_glob=GLOB,
                                    mutate_all=True)
        assert full["summary"]["killed"] == 6
        # 只覆盖 add 体（行 2）→ covered 位点 = 3；位点行全可插桩（D3）
        _seed_projection(proj, target, {2}, {2, 5})
        monkeypatch.setattr(mrunner, "_run_gut_on_project", mk_kill_all())
        filtered = mrunner.run_mutation(str(target), str(proj), budget=10,
                                        tests_glob=GLOB,
                                        mutate_all=True, reuse_coverage=True)
        key = lambda r: (r["summary"]["killed"], r["summary"]["survived"],
                         r["ok"])  # noqa: E731
        # covered 位点（3 个）与全量跑同口径 killed；uncovered 3 显式标记
        assert filtered["summary"]["killed"] == 3
        assert filtered["summary"]["coverage_prefilter"]["uncovered"] == 3
        assert filtered["ok"] is True and full["ok"] is True


class TestCaliperBypassesPrefilter:
    def test_selection_reruns_uncovered_site(self, tmp_path, monkeypatch):
        """P3 卡尺随机抽查被剔位点防假阴性：显式寻址绕过 coverage 前置。"""
        proj, target = _write_project(tmp_path)
        _seed_projection(proj, target, {2}, {2, 5})
        sid = f"{target}#func:sub#1"
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob=GLOB,
                                 select_ids=[sid], reuse_coverage=True)
        # uncovered 位点仍被强制执行（1 baseline + 1 mutant）
        assert calls["n"] == 2
        assert r["summary"]["executed"] == 1
        assert r["ok"] is True
