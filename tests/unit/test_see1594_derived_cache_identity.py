"""SEE-1594 S2（§SPEC-025/027 A04/A05/A06/A11/A12）：派生缓存与身份。

证明：
- A04：/1 手动 store 退出 prefilter 解析链（盘上旧 /1 条目不命中，
  零读链消费者）；
- A05：/2 绑定映射内容与执行证据——同内容命中零 GUT；map 行事实或
  arms_state 变化（同 recipe/面/文件）→ /2 显式失效不命中；
- A06：live projection 用 selection 已解析的 map 路径，不重猜默认；
- A11：tests-face 指纹单实现（coverlink 唯一体，truth_select 委派）；
- A12：行事实按完整 res:// 身份归属——同名不同目录不串 coverage，
  退化形态（裸名）歧义 fail-loud。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import godot_qa_toolkit.mutation.inject as inject
import attribution_fixtures as fx
from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash
from godot_qa_toolkit.mutation import coverlink

TARGET = "utils/t.gd"
SRC = "extends RefCounted\n\nfunc f(a):\n\tif a > 0:\n\t\treturn 1\n\treturn 0\n"
UNIT = "res://tests/unit/test_a.gd"
COMPAT = "res://tests/unit_compat/test_a.gd"


def _proj(tmp_path):
    proj = tmp_path / "proj"
    fx.install_recipe(proj)
    (proj / "utils").mkdir(parents=True)
    (proj / "utils" / "t.gd").write_text(SRC, encoding="utf-8")
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_a.gd").write_text(
        "extends Node\n", encoding="utf-8")
    return proj


def _tests_face(proj) -> dict:
    root = Path(proj)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "tests").rglob("*.gd"))}


def _map_doc(proj, facts: "dict[str, dict[str, list[int]]]", *,
             probes: int = 640) -> dict:
    """facts: 完整 res:// 身份 → {目标 rel: 行集}。写成合法 test-attribution/6。"""
    recipe, error = build_recipe(proj)
    assert error is None, error
    raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
    rel = TARGET
    arm = {"test_to_files": {}, "script_to_files": {}, "tests": 0,
           "scripts": 0, "files": 1, "pairs": 0, "function_pairs": 0,
           "test_to_functions": {}, "script_to_functions": {},
           "file_to_tests": {rel: []}, "file_to_scripts": {rel: []},
           "line_pairs": 0, "script_line_pairs": 0, "test_to_lines": {},
           "script_to_lines": {}, "line_to_tests": {}}
    for ident, by_rel in facts.items():
        lines = by_rel.get(rel) or []
        arm["test_to_files"].setdefault(ident, []).append(rel)
        arm["file_to_tests"][rel] = sorted(
            set(arm["file_to_tests"][rel]) | {ident})
        arm["test_to_lines"][ident] = {rel: list(lines)}
        arm["line_pairs"] += len(lines)
        for l in lines:
            key = str(l)
            arm["line_to_tests"].setdefault(rel, {}).setdefault(key, [])
            if ident not in arm["line_to_tests"][rel][key]:
                arm["line_to_tests"][rel][key].append(ident)
    arm["tests"] = len(arm["test_to_files"])
    arm["pairs"] = sum(len(v) for v in arm["test_to_files"].values())
    return {
        "schema_version": "test-attribution/7",
        "report_recipe_hash": raw, "recipe_hash_pristine": raw,
        "project_root": str(proj),
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {rel: hashlib.sha256(
            (Path(proj) / rel).read_bytes()).hexdigest()},
        "target_files": [rel],
        "tests_face": _tests_face(proj),
        "instrumentable_lines": {rel: [2, 3]},
        # C3-R16/R18：map 必带 feature 内容指纹与 verdict 表（缺键 =
        # 具名 missing 因素走全量重建的新契约）。
        "test_verdicts": {},
        "feature_sha256": {},  # 空面：无 features 目录的夹具 → 对账空集
        "arms": {"gut_full": arm},
        "arms_state": {"gut_full": {"state": "valid", "invalid_reason": None,
                                    "bundles": 1, "pairs": arm["pairs"],
                                    "probes_fired": probes, "vacuous": False}},
        "notes": [],
    }


def _write_map(doc: dict, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _map_lines(doc: dict, ident: str, rel: str) -> list[int]:
    return ((doc["arms"]["gut_full"]["test_to_lines"].get(ident) or {})
            .get(rel) or [])


def _resolve(proj, scripts, **kwargs):
    return coverlink.resolve_coverage_for_prefilter(
        str(proj), str(Path(proj) / TARGET), "res://tests/unit/",
        scripts=scripts, **kwargs)


# ------------------------------------------------------------------ A04


class TestA04ManualStoreRetired:
    """A04：/1 手动 store 退出 prefilter 解析链——盘上 /1 条目永不命中，
    解析链只剩映射派生来源；读链零消费者。"""

    def test_store_entry_on_disk_is_not_consumed(self, tmp_path):
        proj = _proj(tmp_path)
        doc = _map_doc(proj, {UNIT: {TARGET: [2]}})
        map_path = _write_map(doc, Path(proj) / ".qa-cache"
                              / "attribution-map.json")
        # 盘上塞一个 /1 手动条目，行事实与映射不同（{3} vs {2}）
        entry = {"schema": "see1420-coverage/1", "rel": TARGET,
                 "tests_glob": "res://tests/unit/", "binary_kind": "cold",
                 "source_sha1": hashlib.sha1(
                     (Path(proj) / TARGET).read_bytes()).hexdigest(),
                 "total_lines": 2, "covered_lines": [3],
                 "coverage_percent": 50.0, "recorded_at": 0.0,
                 "instrumentable_lines": [2, 3]}
        cache = coverlink.coverage_cache_path(
            str(proj), str(Path(proj) / TARGET), "res://tests/unit/")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(entry), encoding="utf-8")
        cov, inst, reason, source = _resolve(proj, [UNIT], map_path=map_path)
        assert source != "coverage_store"
        assert cov == {2}, "covered facts must come from the map, not /1"

    def test_no_manual_store_readers_left_in_src(self):
        import subprocess
        out = subprocess.run(
            ["grep", "-rn", "-E",
             r"load_reusable_coverage|load_instrumentable_lines|"
             r"_load_coverage_doc",
             "src/"], capture_output=True, text=True)
        assert out.returncode == 1, f"/1 read-chain residue: {out.stdout}"


# ------------------------------------------------------------------ A05


class TestA05ProjectionBinding:
    """A05：/2 绑定映射内容与执行证据——内容/arms_state 变化（同 recipe/
    面/文件）不得命中；同内容命中零 GUT。"""

    def _projected_once(self, proj, monkeypatch):
        doc = _map_doc(proj, {UNIT: {TARGET: [2]}})
        map_path = _write_map(doc, Path(proj) / ".qa-cache"
                              / "attribution-map.json")

        def _boom(*a, **k):
            raise AssertionError("projection must not run GUT")

        monkeypatch.setattr(inject, "spawn_godot", _boom)
        cov, inst, reason, source = _resolve(proj, [UNIT], map_path=map_path)
        assert cov == {2} and source == "projection_live"
        return map_path

    def test_same_content_hits_store_without_gut(self, tmp_path, monkeypatch):
        proj = _proj(tmp_path)
        map_path = self._projected_once(proj, monkeypatch)

        def _boom(*a, **k):
            raise AssertionError("store hit must not run GUT")

        monkeypatch.setattr(inject, "spawn_godot", _boom)
        cov, inst, reason, source = _resolve(proj, [UNIT], map_path=map_path)
        assert source == "projection_store"
        assert cov == {2} and inst == {2, 3}

    def test_line_fact_change_misses_store(self, tmp_path, monkeypatch):
        proj = _proj(tmp_path)
        map_path = self._projected_once(proj, monkeypatch)
        doc = json.loads(Path(map_path).read_text(encoding="utf-8"))
        arm = doc["arms"]["gut_full"]
        arm["test_to_lines"][UNIT][TARGET] = [3]
        _write_map(doc, Path(map_path))
        cov, inst, reason, source = _resolve(proj, [UNIT], map_path=map_path)
        assert source == "projection_live", reason
        assert cov == {3}

    def test_execution_evidence_change_misses_store(
            self, tmp_path, monkeypatch):
        proj = _proj(tmp_path)
        map_path = self._projected_once(proj, monkeypatch)
        doc = json.loads(Path(map_path).read_text(encoding="utf-8"))
        doc["arms_state"]["gut_full"]["probes_fired"] = 100
        _write_map(doc, Path(map_path))
        cov, inst, reason, source = _resolve(proj, [UNIT], map_path=map_path)
        assert source == "projection_live", reason


# ------------------------------------------------------------------ A06


class TestA06ResolvedMapPath:
    """A06：live projection 用调用方传入的 selection 已解析 map，
    显式 map 与默认 map 不串线。"""

    def test_explicit_map_beats_default_map(self, tmp_path):
        proj = _proj(tmp_path)
        default_doc = _map_doc(proj, {UNIT: {TARGET: [2]}})
        _write_map(default_doc, Path(proj) / ".qa-cache"
                   / "attribution-map.json")
        explicit_doc = _map_doc(proj, {UNIT: {TARGET: [3]}})
        explicit_path = _write_map(explicit_doc, Path(proj) / ".qa-cache"
                                   / "other-map.json")
        cov, inst, reason, source = _resolve(proj, [UNIT],
                                             map_path=explicit_path)
        assert cov == {3}, f"must project from the resolved map: {reason}"
        assert source == "projection_live"

    def test_default_map_used_when_no_path_given(self, tmp_path):
        proj = _proj(tmp_path)
        default_doc = _map_doc(proj, {UNIT: {TARGET: [2]}})
        _write_map(default_doc, Path(proj) / ".qa-cache"
                   / "attribution-map.json")
        cov, inst, reason, source = _resolve(proj, [UNIT])
        assert cov == {2} and source == "projection_live"


# ------------------------------------------------------------------ A11


class TestA11TestsFaceSingleImplementation:
    """A11：tests-face rel→sha256 单一实现；内容变化/同名文件两入口一致。"""

    def test_both_entries_agree_same_name_files(self, tmp_path):
        from godot_qa_toolkit.mutation import truth_select
        proj = _proj(tmp_path)
        (proj / "tests" / "unit_compat").mkdir()
        (proj / "tests" / "unit_compat" / "test_a.gd").write_text(
            "extends Node\n", encoding="utf-8")
        face = coverlink.current_tests_face(str(proj))
        cached = truth_select._current_tests_face(str(proj))
        assert face == cached
        assert "tests/unit/test_a.gd" in face
        assert "tests/unit_compat/test_a.gd" in face

    def test_content_change_visible_to_drift_check(self, tmp_path):
        from godot_qa_toolkit.mutation import truth_select
        proj = _proj(tmp_path)
        doc = _map_doc(proj, {UNIT: {TARGET: [2]}})
        before = truth_select._tests_face_drift(str(proj), doc)
        assert before["changed"] == []
        (proj / "tests" / "unit" / "test_a.gd").write_text(
            "extends Node\n# changed\n", encoding="utf-8")
        truth_select._current_tests_face.cache_clear()
        after = truth_select._tests_face_drift(str(proj), doc)
        assert after["changed"] == ["tests/unit/test_a.gd"]
        face = coverlink.current_tests_face(str(proj))
        assert face["tests/unit/test_a.gd"] != doc["tests_face"][
            "tests/unit/test_a.gd"]


# ------------------------------------------------------------------ A12


class TestA12FullIdentityAttribution:
    """A12：行事实身份归属禁 basename 串染。注册表身份 = GUT 裸文件名
    （get_filename() = path.get_file()）；全路径身份精确匹配；裸名身份在
    同名跨目录时歧义 fail-loud，绝不产出跨目录串染的 covered。"""

    def _two_dir_proj(self, tmp_path):
        proj = _proj(tmp_path)
        (proj / "tests" / "unit_compat").mkdir()
        (proj / "tests" / "unit_compat" / "test_a.gd").write_text(
            "extends Node\n", encoding="utf-8")
        return proj

    def test_full_identity_same_basename_no_cross_talk(self, tmp_path):
        proj = self._two_dir_proj(tmp_path)
        doc = _map_doc(proj, {UNIT: {TARGET: [2]}, COMPAT: {TARGET: [3]}})
        map_path = _write_map(doc, Path(proj) / ".qa-cache"
                              / "attribution-map.json")
        cov, inst, reason, source = _resolve(proj, [UNIT], map_path=map_path)
        assert cov == {2}, \
            f"compat dir facts must not leak into unit selection: {cov}"
        assert source == "projection_live"

    def test_bare_identity_same_name_two_dirs_is_named_failure(
            self, tmp_path):
        """运行时真实形态（裸名身份）+ 同名跨目录：归属不可判定 → 具名
        失败，绝不静默并集（修复前 basename 归一产出 {2,3} 串染）。"""
        proj = self._two_dir_proj(tmp_path)
        doc = _map_doc(proj, {"test_a.gd::u": {TARGET: [2]},
                              "test_a.gd::c": {TARGET: [3]}})
        map_path = _write_map(doc, Path(proj) / ".qa-cache"
                              / "attribution-map.json")
        cov, inst, reason, source = _resolve(proj, [UNIT], map_path=map_path)
        assert cov is None
        assert reason is not None and "identity mismatch" in reason
        assert "same-name script" in reason

    def test_bare_selection_script_is_named_failure(self, tmp_path):
        proj = self._two_dir_proj(tmp_path)
        doc = _map_doc(proj, {UNIT: {TARGET: [2]}, COMPAT: {TARGET: [3]}})
        map_path = _write_map(doc, Path(proj) / ".qa-cache"
                              / "attribution-map.json")
        cov, inst, reason, source = _resolve(proj, ["test_a.gd"],
                                             map_path=map_path)
        assert cov is None
        assert reason is not None and "identity mismatch" in reason

    def test_full_identity_exact_match(self, tmp_path):
        proj = self._two_dir_proj(tmp_path)
        doc = _map_doc(proj, {UNIT: {TARGET: [2]}, COMPAT: {TARGET: [3]}})
        map_path = _write_map(doc, Path(proj) / ".qa-cache"
                              / "attribution-map.json")
        cov, inst, reason, source = _resolve(proj, [UNIT, COMPAT],
                                             map_path=map_path)
        assert cov == {2, 3}

    def test_selected_dir_outside_map_face_is_named_failure(self, tmp_path):
        """S2.1 MEDIUM-2：selected `tests2/foo.gd` vs map face `tests/foo.gd`
        ——裸名归属必须在归属函数内部比较目录；旧实现按裸名归属（RED）。"""
        proj = _proj(tmp_path)
        doc = _map_doc(proj, {"foo.gd::t": {TARGET: [2]}})
        map_path = _write_map(doc, Path(proj) / ".qa-cache"
                              / "attribution-map.json")
        cov, inst, reason, source = _resolve(proj, ["res://tests2/foo.gd"],
                                             map_path=map_path)
        assert cov is None, (
            f"selected dir outside map face must not inherit facts: {cov}")
        assert reason is not None and "identity mismatch" in reason


class TestS22SelectionMetadataConsistency:
    """S2.2：runtime_mapping 缺 resolved map path = selection 元数据不一致
    ——三链（line_group/function/chunk）具名 fail-loud，绝不默认重猜；
    planner/executor 缺省参数不得误报 explicit --tests。"""

    def _proj_with_default_map(self, tmp_path):
        from types import SimpleNamespace
        proj = _proj(tmp_path)
        doc = _map_doc(proj, {UNIT: {TARGET: [2]}})
        _write_map(doc, Path(proj) / ".qa-cache" / "attribution-map.json")
        return proj, SimpleNamespace(executed=[])

    def test_line_group_runtime_mapping_without_path_fails_loud(
            self, tmp_path):
        from godot_qa_toolkit.mutation import truth_select
        proj, _ = self._proj_with_default_map(tmp_path)
        file_sel = {"source": "runtime_mapping", "reason": "fresh",
                    "tests": [UNIT], "tests_glob": "res://tests/unit/",
                    "zero_ref": False, "mapping": None,
                    "ambiguous_names": []}
        with pytest.raises(truth_select.MutationSelectionError) as ei:
            truth_select.resolve_line_group_selection(
                str(proj), str(Path(proj) / TARGET), [2],
                file_selection=file_sel)
        assert "selection_metadata_inconsistent" in str(ei.value)

    def test_function_runtime_mapping_without_path_fails_loud(
            self, tmp_path):
        from godot_qa_toolkit.mutation import truth_select
        proj, _ = self._proj_with_default_map(tmp_path)
        file_sel = {"source": "runtime_mapping", "reason": "fresh",
                    "tests": [UNIT], "tests_glob": "res://tests/unit/",
                    "zero_ref": False, "mapping": None,
                    "ambiguous_names": []}
        with pytest.raises(truth_select.MutationSelectionError) as ei:
            truth_select.resolve_function_selection(
                str(proj), str(Path(proj) / TARGET), "f",
                file_selection=file_sel)
        assert "selection_metadata_inconsistent" in str(ei.value)

    def test_omitted_map_path_is_not_misreported_as_explicit_tests(
            self, tmp_path, monkeypatch, capsys):
        """planner/executor 缺省参数（漏穿线）不得落进 explicit-None 分支
        误报 `explicit --tests override`——缺省 = 未解析哨兵（通用回退）。"""
        from godot_qa_toolkit.mutation import planner
        proj, plan = self._proj_with_default_map(tmp_path)
        target = Path(proj) / TARGET
        disclosures: dict = {}
        uncovered, pool = planner._apply_coverage_prefilter(
            plan, str(target), str(proj), "res://tests/unit/", None, True,
            disclosures, identity=None, tests_scripts=[UNIT])
        err = capsys.readouterr().err
        # 缺省 = 未解析哨兵 → 通用回退（默认解析）正常命中，绝不误报显式
        # --tests；「不过滤」语义由显式 None 分支承担
        assert disclosures.get("coverage_prefilter") == "projection_live"
        assert "explicit --tests" not in err


class TestS23Consistency:
    """S2.3：line_group chunk 链具名 fail-loud（不默认重猜）；pathless
    runtime_mapping 的披露与其真实原因对应（不冒充 explicit --tests）。"""

    def _proj_with_default_map(self, tmp_path):
        proj = _proj(tmp_path)
        doc = _map_doc(proj, {UNIT: {TARGET: [2]}})
        _write_map(doc, Path(proj) / ".qa-cache" / "attribution-map.json")
        return proj

    def test_line_group_chunks_runtime_mapping_without_path_fails_loud(
            self, tmp_path):
        """HIGH：`_line_group_chunks` 直读链——runtime_mapping 无 path +
        默认位置存在有效 map → 具名 fail-loud（旧实现静默读默认 map）。"""
        from types import SimpleNamespace

        from godot_qa_toolkit.mutation import executor, truth_select
        proj = self._proj_with_default_map(tmp_path)
        mutants = [SimpleNamespace(line=2, stable_id="calc.gd#func:f#1")]
        selection_meta = {"source": "runtime_mapping", "reason": "fresh",
                          "tests": [UNIT], "tests_glob": "res://tests/unit/",
                          "zero_ref": False, "mapping": None,
                          "ambiguous_names": []}
        with pytest.raises(truth_select.MutationSelectionError) as ei:
            executor._line_group_chunks(
                str(Path(proj) / TARGET), str(proj), "res://tests/unit/",
                None, mutants, selection_meta, None, 2.0, 60, 10,
                False, None, run_id="r", identity=None,
                worker_channel=None, total_sites=1)
        assert "selection_metadata_inconsistent" in str(ei.value)

    def test_selection_map_path_pathless_runtime_mapping_is_inconsistent(
            self, tmp_path):
        """MEDIUM：selection_map_path 单一出处——runtime_mapping 无 path →
        不一致哨兵（消费侧具名披露）；显式 tests → None；有 path → 原样。"""
        from godot_qa_toolkit.mutation import coverlink
        assert coverlink.selection_map_path(
            {"source": "runtime_mapping", "mapping": {}}) \
            is coverlink.MAP_PATH_METADATA_INCONSISTENT
        assert coverlink.selection_map_path(
            {"source": "explicit_tests", "mapping": None}) is None
        assert coverlink.selection_map_path(
            {"source": "runtime_mapping",
             "mapping": {"path": "/tmp/m.json"}}) == "/tmp/m.json"

    def test_selection_map_path_none_mapping_runtime_mapping_is_inconsistent(
            self, tmp_path):
        """S3 首项残余：runtime_mapping + mapping=None 同走不一致哨兵——
        不漏回 explicit-None 文案（ecaf319 上返回 None，RED）。"""
        from godot_qa_toolkit.mutation import coverlink
        assert coverlink.selection_map_path(
            {"source": "runtime_mapping", "mapping": None}) \
            is coverlink.MAP_PATH_METADATA_INCONSISTENT

    def test_resolve_metadata_inconsistent_reason_is_truthful(
            self, tmp_path):
        """不一致哨兵的披露与其真实原因对应——绝不冒充 explicit --tests
        文案。"""
        from godot_qa_toolkit.mutation import coverlink
        proj = self._proj_with_default_map(tmp_path)
        cov, inst, reason, state = coverlink.resolve_coverage_for_prefilter(
            str(proj), str(Path(proj) / TARGET), "res://tests/unit/",
            scripts=[UNIT],
            map_path=coverlink.MAP_PATH_METADATA_INCONSISTENT)
        assert cov is None and inst is None
        assert state == "store_miss"
        assert "selection metadata inconsistent" in (reason or "")
        assert "explicit --tests" not in (reason or "")


class TestS3UnifiedPrefilter:
    """S3（A07/A08）：prefilter 应用单一 helper（planner/executor 同源
    parity）；summary.coverage_prefilter 唯一 writer 形状契约。"""

    def _seeded(self, tmp_path):
        import attribution_fixtures as fx
        from godot_qa_toolkit.mutation.identity import identity_pack
        from godot_qa_toolkit.mutation.inject import detect_binary_kind
        proj = _proj(tmp_path)
        fx.install_recipe(proj)
        (proj / "tests" / "unit" / "test_a.gd").write_text(
            "extends Node\n")
        from godot_qa_toolkit.mutation import truth_select
        truth_select._fresh_recipe_hash.cache_clear()
        fx.seed_projection(
            proj, Path(proj) / TARGET, {UNIT: {TARGET: [2]}},
            scripts=[UNIT], instrumentable={TARGET: [2, 3]},
            identity=identity_pack(str(proj), detect_binary_kind()))
        return proj

    def test_planner_executor_prefilter_parity(self, tmp_path, monkeypatch):
        """A07：同一输入下 planner 与 executor 两条包装产出完全一致
        （uncovered/pool/披露），证明合并后零分叉。"""
        from types import SimpleNamespace

        from godot_qa_toolkit.mutation import executor, planner
        proj = self._seeded(tmp_path)
        target = Path(proj) / TARGET
        mutants = [SimpleNamespace(line=2, stable_id=f"m{m}")
                   for m in (1, 2)] + \
                  [SimpleNamespace(line=3, stable_id=f"m{m}")
                   for m in (3, 4)]
        out = {}
        monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
        for name, fn, path_arg in (
                ("planner", planner._apply_coverage_prefilter, target),
                ("executor", executor._apply_coverage_prefilter, str(target))):
            plan = SimpleNamespace(executed=list(mutants))
            disclosures: dict = {}
            unc, pool = fn(plan, path_arg, str(proj), "res://tests/unit/",
                           None, True, disclosures=disclosures,
                           identity=None, tests_scripts=[UNIT])
            out[name] = ([u.stable_id for u in unc],
                         [p.stable_id for p in pool],
                         dict(disclosures))
        assert out["planner"][:2] == out["executor"][:2]
        # source 依调用顺序 live→store（首个 live 落盘 /2，次个命中缓存）
        # ——两披露同形状同键集，source 均为映射派生来源
        for _, _, disc in out.values():
            pf = disc["coverage_prefilter"]
            assert pf in ("projection_live", "projection_store")
        unc_ids, pool_ids, disc = out["planner"]
        assert set(unc_ids) == {"m3", "m4"}   # line 3 未覆盖 → uncovered
        assert set(pool_ids) == {"m1", "m2"}  # line 2 covered → pool

    def test_no_tests_glob_miss_discloses_and_runs_unfiltered(
            self, tmp_path, capsys):
        """A07 反例：no tests_glob（全脸）→ 具名 miss 披露 + 不过滤。"""
        from types import SimpleNamespace

        from godot_qa_toolkit.mutation import coverlink
        proj = self._seeded(tmp_path)
        mutants = [SimpleNamespace(line=2, stable_id="m1")]
        disclosures: dict = {}
        unc, pool = coverlink.apply_coverage_prefilter(
            mutants, str(Path(proj) / TARGET), str(proj), None, [UNIT],
            None, None, True, disclosures,
            map_path=coverlink.MAP_PATH_UNRESOLVED)
        assert unc == [] and pool == mutants
        assert disclosures["coverage_prefilter"] == "store_miss"
        assert "no tests_glob (full-face audit)" in capsys.readouterr().err

    def test_summary_writer_shape_contract(self):
        """A08：唯一 writer 形状契约——键集恒为 source/uncovered/
        covered_executed。"""
        from godot_qa_toolkit.mutation import coverlink
        assert coverlink.coverage_prefilter_summary(
            "projection_live", 1, 2) == {
            "source": "projection_live", "uncovered": 1,
            "covered_executed": 2}

    def test_disclosure_parity_with_fixed_resolve(self, tmp_path, monkeypatch,
                                                  capsys):
        """S4 首项：固定 resolve 返回——planner/executor 两包装的
        coverage_prefilter 披露逐字段一致，且 stderr 原因行逐字段一致
        （真实 capsys 断言，非仅 membership）。"""
        from types import SimpleNamespace

        from godot_qa_toolkit.mutation import coverlink, executor, planner

        def fixed_resolve(project_root, file_path, tests_glob, scripts=None,
                          binary_kind=None, identity=None, map_path=None):
            # miss 形态：触发 helper 的 stderr 原因行（原因逐字段可比）
            return (None, None, "projection_store: stale entry — reproject",
                    "projection_store")

        monkeypatch.setattr(coverlink, "resolve_coverage_for_prefilter",
                            fixed_resolve)
        proj = self._seeded(tmp_path)
        target = Path(proj) / TARGET
        mutants = [SimpleNamespace(line=2, stable_id="m1"),
                   SimpleNamespace(line=3, stable_id="m2")]
        out = {}
        for name, fn, path_arg in (
                ("planner", planner._apply_coverage_prefilter, target),
                ("executor", executor._apply_coverage_prefilter, str(target))):
            plan = SimpleNamespace(executed=list(mutants))
            disclosures: dict = {}
            unc, pool = fn(plan, path_arg, str(proj), "res://tests/unit/",
                           None, True, disclosures=disclosures,
                           identity=None, tests_scripts=[UNIT])
            err = capsys.readouterr().err
            out[name] = ([u.stable_id for u in unc],
                         [p.stable_id for p in pool],
                         dict(disclosures), err)
        assert out["planner"] == out["executor"], (
            f"disclosure/stderr parity broken: {out}")
        # miss 形态：不过滤（宁多跑），披露保留 resolve 给出的 source
        unc_ids, pool_ids, disc = out["planner"][:3]
        assert unc_ids == [] and pool_ids == ["m1", "m2"]
        assert disc == {"coverage_prefilter": "projection_store"}

    def test_missing_line_is_contract_violation(self, tmp_path):
        """S4 首项：位点缺 line 属性 = 契约违约——显式暴露（AttributeError），
        不静默归入 pool。"""
        from types import SimpleNamespace

        from godot_qa_toolkit.mutation import coverlink
        proj = self._seeded(tmp_path)
        broken = SimpleNamespace(stable_id="m-broken")  # 无 line
        with pytest.raises(AttributeError):
            coverlink.apply_coverage_prefilter(
                [broken], str(Path(proj) / TARGET), str(proj),
                "res://tests/unit/", [UNIT], None, None, True, None,
                map_path=coverlink.MAP_PATH_UNRESOLVED)
